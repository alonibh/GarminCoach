# Git, Test, and Cleanup Protocol

When a task or coding session is complete, you must strictly follow this teardown sequence before finishing:

1. **Test Verification**:
   - Run the full test suite to ensure the new build works and no regressions were introduced.
   - If any test fails, you must stop and fix it. Do not proceed to the next steps until all tests pass.

