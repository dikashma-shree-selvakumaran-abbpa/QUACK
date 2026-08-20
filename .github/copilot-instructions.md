# Copilot Instructions

## Project Guidelines
- For ABB projects, instructions.md is typically the first source of project guidance and should be read early to improve navigation and targeted advice.
- Treat Tier 2 risk labels as non-deterministic/model-dependent advisory signals; explicitly document this as an open item and prioritize calibration metrics and a signed rubric/prompt review before treating HIGH as validated risk.

## Reporting Guidelines
- Display hook durations in quack report titles with one decimal place while keeping the underlying duration value as a float.

## SDK Integration Guidelines
- Suppress SDK logger output and child-log records during both model discovery and completion calls; restore logging state afterward.
- Surface only a one-line normalized/truncated reason with no traceback.