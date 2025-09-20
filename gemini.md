# Gemini Agent Quick Reference

## 🎯 Primary Directive

My purpose is to execute tasks by using my available tools, guided by the project's central knowledge base. My first step in any session is always to query the knowledge base to understand the current state of work.

## 🚀 My Onboarding Workflow (START HERE)

1.  **Query Current State**: I will always start by running `./kb` in `/home/telemetry/telemetry-hub/` to get the current task, status, and context.

    ```bash
    ./kb
    ```

2.  **Understand the Task**: Based on the output, I will use more specific `kb` commands to gather details.
    *   To get an exact command: `./kb cmd <command_name>`
    *   To find relevant files: `./kb files <component>`
    *   To understand a past error: `./kb errors <error_text>`

3.  **Execute**: I will use my core tools (`run_shell_command`, `read_file`, `write_file`, `replace`) to perform the task described in the knowledge base.

## 🤖 How I Interact with Machines

The knowledge base contains information about multiple machines (`BEAST`, `Windows VM`, `TELEMETRY`). My interaction with them is different from a human user.

*   **I DO NOT USE PASSWORDS OR SSH KEYS**: I do not require user/password credentials. My tool environment is already authenticated on the `telemetry` machine.
*   **I USE PRE-CONFIGURED COMMANDS**: When I need to interact with another machine like `BEAST`, I will use the `ssh` commands stored in the `kb`. For example, to run a command on `BEAST`, I will execute:

    ```bash
    # I find this command via "./kb machines beast"
    ssh beast 'some command'
    ```
    
    My `run_shell_command` tool handles the execution of this pre-configured command.

## ⚡ My Core Task Loop: Query, Execute, Update

My entire workflow is a three-step loop:

1.  **Query KB**: What is the goal? (`./kb`)
2.  **Execute Task**: Use my tools to accomplish the goal.
3.  **Update KB**: Document what I did and what the next step is. This is my handoff to the next agent.

    ```bash
    # Example: After finishing a build
    ./kb sql "UPDATE current_state SET task_description='Verification of build output', next_action='./run_tests.sh', status='active'"
    ```

This ensures that the "hive mind" is always up-to-date with my progress.
