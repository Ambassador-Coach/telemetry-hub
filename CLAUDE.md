# Trading System Quick Reference

## 🚀 START HERE - EVERY SESSION

```bash
# 1. Check where you are and what's happening
cd /home/telemetry/telemetry-hub
./kb                    # Shows current task

# 2. Check active projects
./kb sql "SELECT name, status FROM projects WHERE status='active'"

# 3. Check environment if needed
./kb env               # Shows key settings
./kb machines          # Shows how to connect

# 4. Check critical warnings
./kb never             # Things that break production
```

## 📍 ORIENTATION IN 3 SECONDS

**What is this?** Real-time trading system with OCR for order capture  
**Two main components:** TANK (autonomous trading) and telemetry systems  
**Main machines:** BEAST (production), TELEMETRY (dev), Windows VM (build)

## 🎯 COMMON TASKS

```bash
# What am I supposed to do?
./kb

# Get specific command
./kb cmd ocr_rebuild

# Where are files?
./kb files ocr

# Hit an error?
./kb errors [error text]

# Need machine access?
./kb machines beast
```

## ⚠️ CRITICAL RULES

1. **NEVER use Python 3.12** - Only 3.11 works
2. **NEVER pass Python memory to C++** - Use shared memory
3. **NEVER build on wrong platform** - Windows → Windows VM
4. **NEVER touch BEAST directly** - Test first
5. **Run `./kb never` for full list**

## 🏗️ ARCHITECTURE (30-second version)

**BEAST (Windows - Production)**
- TANK trading engine (C:\TANK)
- OCR processing (50ms target)
- Real money, real trades
- Git repo: https://github.com/Ambassador-Coach/TANK-production
- SSH: `ssh beast`

**TELEMETRY (Ubuntu - Development)**  
- Where you are now
- Development environment
- Knowledge base location: /home/telemetry/telemetry-hub
- SSH: Current machine

**Windows VM (Build Environment)**
- Visual Studio, Python 3.11, vcpkg
- Build Windows binaries here
- SSH: `ssh windows-vm`

## 🔐 VERSION CONTROL

**GitHub Organization:** Ambassador-Coach (has Pro features)  
**TANK Repository:** https://github.com/Ambassador-Coach/TANK-production  
- `main` branch: PROTECTED - requires PR  
- `develop` branch: Daily work  
**IMPORTANT:** GitHub Pro is on organization, NOT personal account

## 🔍 DEEP DIVE WHEN NEEDED

```bash
# SQL queries for specific info
./kb sql "SELECT * FROM errors WHERE component='ocr'"
./kb sql "SELECT * FROM files WHERE status='BROKEN'"

# Check build configuration
./kb build

# Full help
./kb help
```

## 📊 DATABASE TABLES (Quick Reference)

### 🔥 Quick Status (`./kb hot` shows all hot areas)
- **current_state** - What's happening NOW + recent_changes (`./kb`)
- **session_handoff** - Where last session left off (`./kb handoff`)
- **projects** - Active work priorities (`./kb sql "SELECT * FROM projects WHERE status='active'"`)

### 📍 Navigation & Setup
- **common_paths** - Where everything is (`./kb paths`)
- **machines** - How to connect (`./kb machines [name]`)
- **dependencies** - What needs installing (`./kb deps [component]`)
- **environment** - System settings (`./kb env [component]`)

### 📝 Commands & Files
- **commands** - Exact commands to run (`./kb cmd <name>`)
- **files** - Component file locations (`./kb files [component]`)
- **build_config** - Build settings (`./kb build`)

### 🚨 Problem Solving
- **errors** - How to fix errors (`./kb errors [text]`)
- **never_do** - Critical mistakes (`./kb never`)
- **solutions** - Proven fixes (`./kb sql "SELECT * FROM solutions WHERE problem LIKE '%keyword%'"`)
- **verification_checklist** - What to verify (`./kb sql "SELECT * FROM verification_checklist WHERE critical=1"`)

### 📚 History & Facts
- **timeline** - Session history (`./kb sql "SELECT * FROM timeline ORDER BY session_num DESC LIMIT 5"`)
- **critical_facts** - Important facts (auto-shown in `./kb`)

## ⚡ KEEP DATABASE CURRENT

**CRITICAL: Update the database when things change!**

```bash
# When task completes or changes:
./kb sql "UPDATE current_state SET task_description='New task', status='active', blocking_issue=NULL"

# When you fix something:
./kb sql "UPDATE files SET status='WORKING' WHERE file_name='accelerator_sharedmem.pyd'"

# When you learn a new lesson:
./kb sql "INSERT INTO never_do (category, bad_action, what_happens, correct_action) VALUES ('deploy', 'Skip testing', 'Production crash', 'Always test first')"

# When you find a solution:
./kb sql "INSERT INTO errors (error_message, component, root_cause, solution) VALUES ('New error', 'ocr', 'Root cause', 'How to fix')"
```

**Before ending session:**
```bash
# Update current state for next session
./kb sql "UPDATE current_state SET task_description='[What needs doing next]', next_action='[Exact command]', last_updated=datetime('now')"
```

## 📚 DETAILED DOCS (if needed)

- `STATE.json` - Current mutable state
- `HISTORY.json` - What worked/failed  
- `RECIPES.json` - Command sequences
- `knowledge.db` - Full SQL database

## 🎯 PHILOSOPHY

**Stop reading, start querying.** Everything you need is in `./kb`.

---
*Last updated: 2025-08-15 - TANK Git repository established*