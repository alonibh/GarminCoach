# Git, Test, and Cleanup Protocol

When a task or coding session is complete, you must strictly follow this teardown sequence before finishing:

1. **Test Verification**:
   - Run the full test suite to ensure the new build works and no regressions were introduced.
   - If any test fails, you must stop and fix it. Do not proceed to the next steps until all tests pass.

2. **Surgical Git Staging**:
   - Identify the exact files modified or created for this specific task.
   - Stage only those relevant files using explicit paths (e.g., `git add path/to/file.js`).
   - **Crucial Prohibition**: Never run `git add .` or `git add -A` as it captures unintended noise.

3. **Discard Irrelevant Changes**:
   - If there are untracked or modified files remaining that were not part of the solution (like temporary debug logs, scrap files, or unrelated workspace changes), clean them up.
   - Ask the user before deleting anything permanent, but default to keeping the workspace clean.

4. **Commit and Push**:
   - Write a clear, concise commit message following conventional commit standards (e.g., `feat: add user authentication flow`).
   - Execute the commit: `git commit -m "..."`
   - Push the changes to the remote repository: `git push`
