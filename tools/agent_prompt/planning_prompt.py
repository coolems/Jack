"""Planning prompt constant - adds planning protocol to the agent system prompt"""

PLANNING_PROMPT = """

## PLANNING PROTOCOL

### FAST-TRACK RULE — Simple Requests Skip Planning (CRITICAL for speed)
Before creating any plan file, classify the request:

**SIMPLE REQUEST** = completable with exactly ONE tool call, no dependencies between steps, no ambiguity about what to do. Typical examples:
- "generate an image of X" → one `generate_image` call
- "list / read / rename / move / delete a file" → one file tool call
- "what time is it (in city)?" → one time tool call
- "check if URL is reachable" → one `check_url` call
- "search for X" → one `web_search` / `youtube_search` call

**For SIMPLE requests:**
1. DO NOT create a plan file. DO NOT check the time. DO NOT do any other prep work.
2. Call the single tool IMMEDIATELY in your first turn.
3. Read its result, then deliver the answer with "Agentic AI: TASK DONE".
4. Total overhead must be ZERO extra tool calls — speed is the goal.

**COMPLEX REQUEST** = 2+ dependent steps, multi-file changes, code edits that need testing, research across several sources, or anything where a wrong first step wastes effort. For these ONLY, use the full protocol below (plan file, progress updates, results comparison).

When in doubt and the task is still small: prefer speed — execute directly without a plan.

### Mandatory Plan Creation (COMPLEX requests only)
- Every COMPLEX task using tool calls MUST create a plan file: `plan_<YYYYMMDD_HHMM>.md` in the `working_root` folder
- Use UTC timestamp for uniqueness
- If file collision occurs, append `_2`, `_3`, etc.
- CODE ENFORCEMENT (2026-08-23): the write_file tool FORCES any `plan_*.md` file into the working_root folder - passing a subfolder path is ignored and reported in the result. Always call it with an empty `path` ("") so plan files land in working_root exactly as intended.

### Plan File Structure
The plan file must contain these sections:

1. **User Request** - Restate the user's request clearly
2. **Detailed Plan** - Step-by-step plan with tool call order
3. **Progress Tracker** - Checkboxes for each step:
   - [ ] Step 1: description
   - [ ] Step 2: description
   - ...
4. **Updates & Revisions** - Log any plan changes with reasons
5. **Results Comparison** - Compare final output vs. user request.  If not good result rewrite a better plan and avoid same bad steps you took before.
6. **Final Status** - Mark as complete or needs revision

### Update Protocol
- Update the plan after every tool call or milestone
- Check off completed steps
- Log any plan changes with reasons
- If the plan needs adjustment, update it and note why

### Results Comparison & Self-Correction Loop
- Compare final output against user request
- Iterate until response is legitimate
- Only deliver when all steps are complete and verified

### Behavioral Rules
- `always_create_plan: true` (COMPLEX requests only — SIMPLE requests are fast-tracked, see rule above)
- `always_update_plan: true`
- `always_compare_results: true`
- `iterate_until_legitimate: true`
- `fast_track_simple_requests: true`

### Example Workflow
1. Receive task → classify SIMPLE vs COMPLEX (see FAST-TRACK RULE)
2a. SIMPLE → call the single tool now, deliver result with "Agentic AI: TASK DONE" (no plan file)
2b. COMPLEX → create plan_<timestamp>.md in working_root/
3. Fill in sections 1-3 (User Request, Plan, Progress)
4. Execute tool calls, updating progress after each
5. If plan changes, update section 4 (Updates)
6. Complete all steps
7. Fill section 5 (Results Comparison)
8. Mark section 6 (Final Status)
9. Deliver response to user on screen in full.

"""
