# CEO Bot - Intelligent Bot Orchestrator for Jeffco Fibres

An AI-powered orchestration system that manages multiple specialized bots for email, Teams, EDI, and database operations with built-in learning and continuous improvement.

## 🤖 Managed Bots

### 1. **InboxBot** (Outlook Email)
- Processes Outlook inbox using existing `inbox_bot.py`
- Creates draft email replies with Ollama LLM
- Routes emails: Automated Alerts, Filed, To Reply, Waiting
- Filters noise, handles tickets, prioritizes internal emails

### 2. **TeamsBot** (Microsoft Teams)
- Processes Teams DMs using existing `teams_bot.py`
- Creates draft replies for unread direct messages
- UI automation for Teams Desktop app
- Focuses on 1:1 conversations, skips group chats

### 3. **SQLBot** (Database Operations)
- Executes SQL queries on VM database
- Answers ticketing questions using natural language
- Safety checks for destructive operations
- Converts questions to SQL automatically

### 4. **EDIBot** (EDI Templates + Tray.io)
- Creates one-shot EDI templates from customer specs
- Manages and triggers Tray.io workflows via API
- Maintains an EDI memory log

### 5. **ChromeBot** (Tray.io Knowledge Graph)
- Handles tray.io workflow status checks
- Triggers EDI synchronization (knowledge graph based)
- Monitors workflow status

## 🧠 Learning System

The CEO Bot continuously learns and improves:

- **Performance Tracking** - Success rates, error counts, execution times
- **Feedback Collection** - Rate bot actions 1-5 for quality
- **AI Analysis** - LLM generates improvement suggestions
- **Automated Reports** - Daily and weekly performance summaries

### Learning Schedule
- **Daily 6:00 PM** - Performance analysis & suggestions
- **Monday 9:00 AM** - Weekly learning cycle & improvements
- **Continuous** - Real-time performance tracking

## 📧 Morning Reports

Automatic email to `rfoley@jeffcofibres.com` at 8:00 AM daily:

```
Daily Work Summary - 2026-02-25
================================

INBOX BOT (15 tasks)
  [08:45] Process inbox: completed → Processed 12 emails, 7 drafts
  [10:30] Process inbox: completed → Processed 8 emails, 4 drafts

TEAMS BOT (8 tasks)
  [09:15] Process Teams: completed → 3 DMs replied
  [11:00] Process Teams: completed → 2 DMs replied

PERFORMANCE:
  InboxBot: 95% success, 4.2/5 avg rating
  TeamsBot: 100% success, 4.5/5 avg rating
```

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install ollama requests pywin32 pywinauto schedule python-dotenv
```

### 2. Configure Environment
```bash
cp .env .env
# Edit .env with your credentials
```

### 3. Setup Ollama
```bash
ollama pull llama3.1
ollama pull qwen2.5:7b-instruct
```

### 4. Run CEO Bot
```bash
python CEO.py
```

## 📋 Configuration Required

### Essential (.env file)
- `SMTP_USER` - Gmail address for sending reports
- `SMTP_PASSWORD` - Gmail app password (not regular password!)

### Optional
- `DB_HOST, DB_USER, DB_PASSWORD` - For SQL Bot
- `TRAY_IO_USERNAME, TRAY_IO_PASSWORD` - For Chrome Bot EDI
- `TRAY_IO_API_TOKEN` - For EDIBot Tray.io API management
- `TRAY_IO_API_BASE` - Override Tray.io API base URL (defaults to TRAY_IO_URL/api/v1)
- `EDI_MODEL` - Ollama model name for EDI template generation
- Other settings use sensible defaults

### Existing Bot Configs
- `C:\Users\rfoley\PycharmProjects\InboxBot\inbox_bot.config.json`
- `C:\Users\rfoley\PycharmProjects\InboxBot\teams_bot.config.json`

## 🎮 Interactive Commands

```bash
CEO Bot > morning report          # Send daily summary
CEO Bot > check bots             # Check all bot status
CEO Bot > process inbox          # Run inbox bot once
CEO Bot > process teams          # Run teams bot once
CEO Bot > show performance       # View bot metrics
CEO Bot > generate improvements  # Get AI suggestions
CEO Bot > rate InboxBot last action 5 "Excellent!"
```

## 📊 Scheduled Tasks

| Time | Task | Description |
|------|------|-------------|
| 8:00 AM Daily | Morning Report | Email work summary to you |
| Every 4 hours | Bot Status Check | Verify all bots operational |
| 6:00 PM Daily | Performance Analysis | Analyze metrics & suggest improvements |
| 9:00 AM Monday | Learning Cycle | Deep analysis & send learning report |

## 📁 Project Structure

```
MorningTaskBot/
├── CEO.py                    # Main orchestrator
├── edi_bot.py                # EDI template + Tray.io management
├── .env                      # Configuration (create from .env.example)
├── .env.example             # Template configuration
├── SETUP.md                 # Detailed setup instructions
├── LEARNING.md              # Learning system documentation
├── work_log.json            # All bot activities
├── edi_memory.jsonl          # EDI bot memory log
├── feedback.json            # User ratings
├── analytics.json           # Performance metrics
├── improvements.json        # AI-generated suggestions
└── ceo_bot.log             # Debug/error logs

InboxBot/
├── inbox_bot.py             # Email processing (existing)
├── teams_bot.py             # Teams DM processing (existing)
├── inbox_bot.config.json    # Inbox bot settings
└── teams_bot.config.json    # Teams bot settings
```

## 🔧 Customization

### Change Schedule Times
Edit `run_scheduled_tasks()` in CEO.py:
```python
schedule.every().day.at("07:00").do(run_morning_routine)  # 7 AM instead of 8 AM
```

### Disable Learning
```python
LEARNING_ENABLED = False  # In CEO.py
```

### Change LLM Model
```python
MODEL = "qwen2.5:14b-instruct"  # More capable model
```

### Add Custom Bot
Extend the CEO Bot with new bots:
```python
class CustomBot:
    def __init__(self):
        self.name = "CustomBot"

    def do_task(self) -> Dict[str, Any]:
        log_work_item(self.name, "Task", "completed", "Details")
        return {"ok": True}
```

## 📚 Documentation

- **[SETUP.md](SETUP.md)** - Complete setup guide
- **[LEARNING.md](LEARNING.md)** - Learning system deep dive
- **[.env.example](.env)** - All configuration options

## 🔍 Monitoring & Logs

### Real-time Logs
```bash
tail -f ceo_bot.log
```

### View Work History
```bash
cat work_log.json | jq '.[-10:]'  # Last 10 items
```

### Check Performance
```bash
cat analytics.json | jq
```

### Review Feedback
```bash
cat feedback.json | jq '.[-5:]'  # Last 5 ratings
```

## 🐛 Troubleshooting

### Bot Not Running
1. Check Outlook/Teams Desktop apps are open
2. Verify Ollama is running: `ollama list`
3. Check ceo_bot.log for errors
4. Ensure configs exist in InboxBot folder

### No Morning Emails
1. Verify SMTP credentials in .env
2. Check Gmail app password is enabled
3. Test: `CEO Bot > morning report`
4. Check ceo_bot.log for email errors

### Performance Issues
1. Check if bots have sufficient permissions
2. Verify network connectivity for EDI/DB
3. Ensure Ollama model is downloaded
4. Monitor CPU/RAM usage

## 🎯 Best Practices

1. **Review Drafts** - Bots create drafts, you send them (safety!)
2. **Rate Actions** - Provide feedback to improve performance
3. **Check Logs** - Monitor ceo_bot.log for issues
4. **Test Changes** - Use dry-run mode when testing
5. **Backup Configs** - Keep copies of working configurations

## 🔐 Security

- All credentials stored in .env (gitignored)
- Bots never send emails automatically (draft only)
- SQL Bot has safety checks for destructive queries
- Read-only database access recommended
- Local processing (no cloud sync)

## 📈 Metrics & KPIs

The CEO Bot tracks:
- **Success Rate** - % of successful bot operations
- **Response Time** - Average execution time
- **User Satisfaction** - Average rating from feedback
- **Error Rate** - Failed operations per day
- **Draft Quality** - Feedback ratings on generated content

## 🤝 Integration Points

### Existing Bots
- `inbox_bot.py` - Fully integrated ✅
- `teams_bot.py` - Fully integrated ✅

### Ready for Integration
- Tray.io API (ChromeBot)
- SQL Database (SQLBot)
- Additional custom bots

### Future Extensions
- Slack integration
- Ticket system API
- Cloud storage monitoring
- Calendar management

## 📝 Version History

**v1.0** (Current)
- Initial release
- Inbox & Teams bot integration
- Learning system
- Performance analytics
- Scheduled reports

## 🙋 Support

For issues or questions:
1. Check SETUP.md and LEARNING.md
2. Review ceo_bot.log for errors
3. Verify all prerequisites are installed
4. Test components individually

## 🎉 Success Criteria

You'll know it's working when:
- ✅ Morning email arrives at 8:00 AM
- ✅ Drafts appear in Outlook/Teams
- ✅ work_log.json shows recent activity
- ✅ No errors in ceo_bot.log
- ✅ Performance metrics are positive

---

**Built for Jeffco Fibres** | Powered by Ollama & Claude Code
