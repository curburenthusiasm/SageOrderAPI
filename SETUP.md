# CEO Bot Setup Guide

## Prerequisites

### 1. Software Requirements
- **Windows OS** (for Outlook and Teams integration)
- **Python 3.8+**
- **Ollama** installed and running: https://ollama.ai
- **Microsoft Outlook Desktop** (configured with your account)
- **Microsoft Teams Desktop** (configured and running)

### 2. Python Dependencies
```bash
pip install ollama requests pywin32 pywinauto schedule python-dotenv
```

### 3. Ollama Model Setup
Pull the model used by the bots:
```bash
ollama pull llama3.1
# Or for the inbox/teams bots:
ollama pull qwen2.5:7b-instruct
```

## Configuration Files

### 1. CEO Bot Configuration (.env)
Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env .env
# Then edit .env with your actual credentials
```

**Required settings:**
- `SMTP_USER` and `SMTP_PASSWORD` - For sending morning reports
- Email must be configured for app-specific passwords (Gmail)

**Optional settings:**
- Database credentials (if using SQL Bot)
- Tray.io credentials (if using Chrome Bot for EDI)

### 2. Inbox Bot Configuration
The inbox bot uses: `C:\Users\rfoley\PycharmProjects\InboxBot\inbox_bot.config.json`

**Key settings to verify:**
```json
{
  "internal_domain": "jeffcofibres.com",
  "ollama_model": "qwen2.5:7b-instruct",
  "lookback_days": 20,
  "loop_seconds": 60
}
```

### 3. Teams Bot Configuration
The teams bot uses: `C:\Users\rfoley\PycharmProjects\InboxBot\teams_bot.config.json`

**Key settings to verify:**
```json
{
  "ollama_model": "qwen2.5:7b-instruct",
  "loop_seconds": 45,
  "dm_only": true
}
```

## Gmail App Password Setup

If using Gmail for SMTP:

1. Go to Google Account settings
2. Security → 2-Step Verification (enable it)
3. Security → App passwords
4. Generate app password for "Mail"
5. Copy the 16-character password to `.env` as `SMTP_PASSWORD`

## Database Setup (SQL Bot)

If using the SQL bot:

1. Install database driver:
   ```bash
   # For SQL Server
   pip install pyodbc
   # Or for MySQL
   pip install pymysql
   # Or for PostgreSQL
   pip install psycopg2
   ```

2. Configure connection in `.env`:
   ```
   DB_HOST=your_vm_ip
   DB_USER=readonly_user
   DB_PASSWORD=secure_password
   DB_NAME=ticketing_db
   ```

## Running the CEO Bot

### Option 1: Manual Run
```bash
cd C:\Users\rfoley\PycharmProjects\MorningTaskBot
python CEO.py
```

### Option 2: Windows Task Scheduler (Auto-start)

1. Open Task Scheduler
2. Create Basic Task
3. Name: "CEO Bot"
4. Trigger: At startup
5. Action: Start a program
   - Program: `python.exe`
   - Arguments: `C:\Users\rfoley\PycharmProjects\MorningTaskBot\CEO.py`
   - Start in: `C:\Users\rfoley\PycharmProjects\MorningTaskBot`

### Option 3: As a Windows Service

Install NSSM (Non-Sucking Service Manager):
```bash
# Download from https://nssm.cc/download
nssm install CEOBot "C:\Python3\python.exe" "C:\Users\rfoley\PycharmProjects\MorningTaskBot\CEO.py"
nssm start CEOBot
```

## Testing the Setup

### 1. Test Ollama
```bash
curl http://localhost:11434/api/tags
```

### 2. Test Email Sending
Run CEO bot and type:
```
morning report
```

### 3. Test Inbox Bot
Run CEO bot and type:
```
process inbox
```

### 4. Test Teams Bot
Run CEO bot and type:
```
process teams
```

## Monitoring

### Log Files
- `ceo_bot.log` - Main CEO bot log
- `work_log.json` - All bot activities
- `logs/actions.jsonl` - Inbox bot actions
- `logs/teams_actions.jsonl` - Teams bot actions
- `logs/llm_calls.jsonl` - LLM interactions

### Daily Reports
Check your email (rfoley@jeffcofibres.com) at 8:00 AM for daily summaries.

## Troubleshooting

### "Outlook not found"
- Ensure Outlook Desktop is installed and configured
- Try running Outlook manually first

### "Teams not found"
- Ensure Teams Desktop is running
- Try opening Teams manually first

### "Ollama connection failed"
- Check if Ollama is running: `ollama list`
- Restart Ollama service if needed

### "Email sending failed"
- Verify SMTP credentials in `.env`
- Check if app password is enabled (Gmail)
- Test with a simple email script first

### "Permission denied" errors
- Run as Administrator (right-click → Run as administrator)
- Check antivirus isn't blocking Python

## Security Notes

1. **Never commit .env file** - Contains sensitive passwords
2. **Use read-only database accounts** for SQL Bot
3. **Restrict email permissions** to only what's needed
4. **Review draft emails** before sending manually
5. **Monitor logs** for unexpected behavior

## Next Steps

Once setup is complete:
1. Let CEO bot run for 24 hours to collect data
2. Review the first morning report
3. Adjust bot configurations based on performance
4. Enable learning mode (see LEARNING.md)
