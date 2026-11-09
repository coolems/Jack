"""Planning prompt constant - lightweight planning and continuation protocol"""

PLANNING_PROMPT = """

## PLANNING
- Simple request (zero or one tool call, no dependent steps): act immediately. No plan file, no time check, no extra preparation reply.
- Complex request: think through a short ordered plan before acting, then execute it. Keep the plan in context; do not write or update a plan file after every tool call.
- Write a plan file only when the work is long-running enough to span turns, a system notice asks for a continuation/recovery plan, or the user asks for one.
- Continuation plan: `.temp/plans/plan_<YYYYMMDD_HHMM>_continue.md` via write_file with `path=''` and the bare filename. Capture what is done, the exact remaining steps, and the decisions/context needed to resume. Then give a final text answer.
- Compare the result against the user's request before delivering. If a step failed or the result is wrong, correct it rather than repeating the same failed action.

"""
