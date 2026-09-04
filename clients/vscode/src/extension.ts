import * as vscode from "vscode";

interface Finding {
    severity: string;
    source: string;
    rule: string;
    file: string;
    line: number;
    message: string;
}

interface StatusResponse {
    schemaVersion: number;
    version: string;
    availabilityError: string | null;
    cachePath: string;
    buildCommit: string | null;
    buildDate: string | null;
}

interface CheckResponse {
    schemaVersion: number;
    repository: string;
    blocked: boolean;
    findings: Finding[];
    aiError: string | null;
}

interface AgentStages {
    tier1: { blocked: boolean; findings: Finding[]; note?: string } | null;
    tier2: {
        available: boolean;
        model?: string;
        risk?: string;
        summary?: string;
        reasons?: string[];
        testsToRun?: string[];
        missingTests?: string[];
        error?: string;
    } | null;
    investigation: {
        summary: string;
        testsRun: string[];
        failures: { test: string; diagnosis: string }[];
        proposedPatch: string | null;
        proposedNewTests: string | null;
    } | null;
}

interface AgentJob {
    schemaVersion: number;
    jobId: string;
    status: "running" | "done" | "error" | "cancelled";
    repoPath: string;
    stages: AgentStages;
    error: string | null;
}

let output: vscode.OutputChannel;
let diagnostics: vscode.DiagnosticCollection;
let statusBar: vscode.StatusBarItem;

function serverUrl(): string {
    return vscode.workspace
        .getConfiguration("quack")
        .get<string>("serverUrl", "http://127.0.0.1:8787")
        .replace(/\/+$/, "");
}

function workspaceRoot(): string | undefined {
    const folders = vscode.workspace.workspaceFolders;
    return folders && folders.length > 0 ? folders[0].uri.fsPath : undefined;
}

// The server returns "error" for blocking checks and "warn" for advisory ones.
function toSeverity(value: string): vscode.DiagnosticSeverity {
    return value === "error"
        ? vscode.DiagnosticSeverity.Error
        : vscode.DiagnosticSeverity.Warning;
}

// Quack lines are 1-indexed; VS Code is 0-indexed. File-level findings such as
// large_file report line 0, which must not become -1.
function toRange(line: number): vscode.Range {
    const zeroBased = Math.max(0, line - 1);
    return new vscode.Range(zeroBased, 0, zeroBased, Number.MAX_SAFE_INTEGER);
}

async function post(path: string, body: unknown): Promise<any> {
    const res = await fetch(`${serverUrl()}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    const text = await res.text();
    let parsed: any;
    try {
        parsed = JSON.parse(text);
    } catch {
        throw new Error(`${res.status}: ${text.slice(0, 200)}`);
    }
    if (!res.ok) {
        // Surface the server's own reason rather than a generic failure.
        throw new Error(parsed?.detail ?? `${res.status}`);
    }
    return parsed;
}

async function refreshStatus(): Promise<void> {
    const chosen = selectedModel();
    const modelLine = chosen ? `Model: ${chosen}` : "Model: server default";
    try {
        const res = await fetch(`${serverUrl()}/status`);
        const data = (await res.json()) as StatusResponse;
        const build = data.buildCommit ? ` (${data.buildCommit})` : "";
        statusBar.text = `$(check) quack ${data.version}${build}`;
        statusBar.tooltip = data.availabilityError
            ? `Provider problem: ${data.availabilityError}\n${modelLine}`
            : `Connected to ${serverUrl()}\n${modelLine}`;
    } catch {
        statusBar.text = "$(circle-slash) quack offline";
        statusBar.tooltip = `No quack server at ${serverUrl()} - run 'quack serve'\n${modelLine}`;
    }
    statusBar.show();
}

async function runCheck(): Promise<void> {
    const root = workspaceRoot();
    if (!root) {
        vscode.window.showWarningMessage("quack: open a folder first.");
        return;
    }

    let data: CheckResponse;
    try {
        data = await post("/check", { repo_path: root });
    } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        vscode.window.showErrorMessage(
            message.includes("fetch")
                ? `quack: no server at ${serverUrl()} - run 'quack serve'`
                : `quack: ${message}`
        );
        await refreshStatus();
        return;
    }

    diagnostics.clear();
    const byFile = new Map<string, vscode.Diagnostic[]>();
    for (const f of data.findings) {
        const diagnostic = new vscode.Diagnostic(
            toRange(f.line),
            f.message,
            toSeverity(f.severity)
        );
        diagnostic.source = `quack:${f.source}`;
        diagnostic.code = f.rule;
        const list = byFile.get(f.file) ?? [];
        list.push(diagnostic);
        byFile.set(f.file, list);
    }
    for (const [file, list] of byFile) {
        diagnostics.set(vscode.Uri.file(`${root}/${file}`), list);
    }

    if (data.blocked) {
        vscode.window.showErrorMessage(
            `quack: commit blocked - ${data.findings.length} finding(s).`
        );
    } else if (data.findings.length > 0) {
        vscode.window.showWarningMessage(
            `quack: ${data.findings.length} advisory finding(s).`
        );
    } else {
        vscode.window.showInformationMessage("quack: no findings.");
    }
    await refreshStatus();
}

interface ModelsResponse {
    schemaVersion: number;
    defaultModel: string;
    models: { id: string; displayName: string; provider: string }[];
}

function selectedModel(): string {
    return vscode.workspace.getConfiguration("quack").get<string>("model", "").trim();
}

async function selectModel(): Promise<void> {
    let data: ModelsResponse;
    try {
        const res = await fetch(`${serverUrl()}/models`);
        data = (await res.json()) as ModelsResponse;
    } catch {
        vscode.window.showErrorMessage(
            `quack: no server at ${serverUrl()} - run 'quack serve'`
        );
        return;
    }

    const current = selectedModel();
    const items: vscode.QuickPickItem[] = [
        {
            label: "Use the server default",
            description: data.defaultModel ? `currently ${data.defaultModel}` : "",
            detail: current === "" ? "selected" : undefined,
        },
        ...data.models.map((m) => ({
            label: m.id,
            detail: m.id === current ? "selected" : undefined,
        })),
    ];

    const picked = await vscode.window.showQuickPick(items, {
        title: "QUACK: model for AI review and investigation",
        placeHolder: "Larger models cost more per run",
    });
    if (!picked) {
        return;
    }

    const value = picked.label === "Use the server default" ? "" : picked.label;
    await vscode.workspace
        .getConfiguration("quack")
        .update("model", value, vscode.ConfigurationTarget.Workspace);
    vscode.window.showInformationMessage(
        value ? `quack: using ${value}` : "quack: using the server default"
    );
    await refreshStatus();
}
function reportMarkdown(job: AgentJob, model: string): string {
    const lines: string[] = [];
    lines.push(`# quack review`);
    lines.push("");
    lines.push(`\`${job.repoPath}\``);
    lines.push("");

    const t1 = job.stages.tier1;
    if (t1) {
        lines.push("## Local checks");
        lines.push("");
        if (t1.note) {
            lines.push(t1.note);
        } else if (t1.blocked) {
            lines.push(`**Blocked** by ${t1.findings.length} finding(s). No diff was sent to any model.`);
            lines.push("");
            lines.push("| File | Line | Rule | Message |");
            lines.push("|---|---|---|---|");
            for (const f of t1.findings) {
                lines.push(`| \`${f.file}\` | ${f.line} | ${f.rule} | ${f.message} |`);
            }
        } else {
            lines.push("Clean.");
        }
        lines.push("");
    }

    const t2 = job.stages.tier2;
    if (t2) {
        lines.push("## AI review");
        lines.push("");
        if (t2.available) {
            lines.push(`**Risk: ${t2.risk}** · ${t2.model}`);
            lines.push("");
            lines.push(t2.summary ?? "");
            lines.push("");
            for (const r of t2.reasons ?? []) {
                lines.push(`- ${r}`);
            }
            for (const m of t2.missingTests ?? []) {
                lines.push(`- no test covers \`${m}\``);
            }
        } else {
            lines.push(`Unavailable: ${t2.error}`);
        }
        lines.push("");
    }

    const inv = job.stages.investigation;
    if (inv) {
        lines.push("## Investigation");
        lines.push("");
        lines.push(inv.summary);
        lines.push("");
        if (inv.testsRun.length > 0) {
            lines.push("**Tests run**");
            lines.push("");
            for (const test of inv.testsRun) {
                lines.push(`- \`${test}\``);
            }
            lines.push("");
        }
        for (const f of inv.failures) {
            lines.push(`**FAIL** \`${f.test}\` — ${f.diagnosis}`);
            lines.push("");
        }
        if (inv.proposedNewTests) {
            lines.push("## Proposed tests");
            lines.push("");
            lines.push("```");
            lines.push(inv.proposedNewTests);
            lines.push("```");
            lines.push("");
        }
        if (inv.proposedPatch) {
            lines.push("## Proposed patch");
            lines.push("");
            lines.push("Not applied.");
            lines.push("");
            lines.push("```diff");
            lines.push(inv.proposedPatch);
            lines.push("```");
            lines.push("");
        }
    }

    if (job.error) {
        lines.push("## Error");
        lines.push("");
        lines.push(job.error);
    }

    return lines.join("\n");
}

async function openReport(job: AgentJob, model: string): Promise<void> {
    const doc = await vscode.workspace.openTextDocument({
        content: reportMarkdown(job, model),
        language: "markdown",
    });
    await vscode.window.showTextDocument(doc, { preview: false });
}
function renderStages(job: AgentJob, shown: Set<string>): void {
    const t1 = job.stages.tier1;
    if (t1 && !shown.has("tier1")) {
        shown.add("tier1");
        if (t1.note) {
            output.appendLine(t1.note);
        } else if (t1.blocked) {
            output.appendLine(`BLOCKED by ${t1.findings.length} tier 1 finding(s):`);
            for (const f of t1.findings) {
                output.appendLine(`  ${f.file}:${f.line}  ${f.rule}  ${f.message}`);
            }
            output.appendLine("No diff was sent to any model.");
        } else {
            output.appendLine("Tier 1: clean");
        }
    }

    const t2 = job.stages.tier2;
    if (t2 && !shown.has("tier2")) {
        shown.add("tier2");
        output.appendLine("");
        if (t2.available) {
            output.appendLine(`AI review (${t2.model}) - risk: ${t2.risk}`);
            output.appendLine(`  ${t2.summary}`);
            for (const r of t2.reasons ?? []) {
                output.appendLine(`  - ${r}`);
            }
            for (const m of t2.missingTests ?? []) {
                output.appendLine(`  no test covers ${m}`);
            }
        } else {
            output.appendLine(`AI review unavailable (${t2.error})`);
        }
    }

    const inv = job.stages.investigation;
    if (inv && !shown.has("investigation")) {
        shown.add("investigation");
        output.appendLine("");
        output.appendLine("Investigation");
        output.appendLine(`  ${inv.summary}`);
        for (const test of inv.testsRun) {
            output.appendLine(`  ran: ${test}`);
        }
        for (const f of inv.failures) {
            output.appendLine(`  FAIL ${f.test}: ${f.diagnosis}`);
        }
        if (inv.proposedNewTests) {
            output.appendLine("");
            output.appendLine("Proposed tests:");
            output.appendLine(inv.proposedNewTests);
        }
        if (inv.proposedPatch) {
            output.appendLine("");
            output.appendLine("Proposed patch (not applied):");
            output.appendLine(inv.proposedPatch);
        }
    }
}

async function runAgent(): Promise<void> {
    const root = workspaceRoot();
    if (!root) {
        vscode.window.showWarningMessage("quack: open a folder first.");
        return;
    }

    const model = selectedModel();
    let job: AgentJob;
    try {
        job = await post("/agent", model ? { repo_path: root, model } : { repo_path: root });
    } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        vscode.window.showErrorMessage(`quack: ${message}`);
        return;
    }

    output.clear();
    output.show(true);
    output.appendLine(`quack agent - ${root}`);
    output.appendLine("");

    await vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title: "quack: analyzing",
            cancellable: true,
        },
        async (progress, token) => {
            const shown = new Set<string>();
            token.onCancellationRequested(() => {
                void fetch(`${serverUrl()}/agent/${job.jobId}`, { method: "DELETE" });
            });

            for (;;) {
                await new Promise((r) => setTimeout(r, 2000));
                if (token.isCancellationRequested) {
                    return;
                }
                let current: AgentJob;
                try {
                    const res = await fetch(`${serverUrl()}/agent/${job.jobId}`);
                    current = (await res.json()) as AgentJob;
                } catch {
                    output.appendLine("lost contact with the quack server");
                    return;
                }
                renderStages(current, shown);
                progress.report({
                    message: current.stages.tier2 ? "investigating" : "reviewing changes",
                });
                if (current.status !== "running") {
                    if (current.status === "error") {
                        output.appendLine("");
                        output.appendLine(`error: ${current.error}`);
                    }
                    if (current.status === "done") {
                        await openReport(current, model);
                    }
                    return;
                }
            }
        }
    );
}
export function activate(context: vscode.ExtensionContext): void {
    output = vscode.window.createOutputChannel("quack");
    diagnostics = vscode.languages.createDiagnosticCollection("quack");
    statusBar = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Left,
        100
    );
    statusBar.command = "quack.check";
    context.subscriptions.push(
        diagnostics,
        statusBar,
        vscode.commands.registerCommand("quack.check", runCheck),
        vscode.commands.registerCommand("quack.agent", runAgent),
        vscode.commands.registerCommand("quack.selectModel", selectModel),
        output
    );
    void refreshStatus();
}

export function deactivate(): void {
    diagnostics?.dispose();
    statusBar?.dispose();
}