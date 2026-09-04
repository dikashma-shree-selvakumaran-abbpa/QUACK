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
    try {
        const res = await fetch(`${serverUrl()}/status`);
        const data = (await res.json()) as StatusResponse;
        const build = data.buildCommit ? ` (${data.buildCommit})` : "";
        statusBar.text = `$(check) quack ${data.version}${build}`;
        statusBar.tooltip = data.availabilityError
            ? `Provider problem: ${data.availabilityError}`
            : `Connected to ${serverUrl()}`;
    } catch {
        statusBar.text = "$(circle-slash) quack offline";
        statusBar.tooltip = `No quack server at ${serverUrl()} - run 'quack serve'`;
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

    let job: AgentJob;
    try {
        job = await post("/agent", { repo_path: root });
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
        output
    );
    void refreshStatus();
}

export function deactivate(): void {
    diagnostics?.dispose();
    statusBar?.dispose();
}