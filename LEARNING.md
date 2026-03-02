# CEO Bot Learning & Improvement System

## Overview

The CEO Bot includes an AI-powered learning system that continuously monitors bot performance, collects feedback, and generates improvement suggestions.

## How It Works

### 1. Performance Tracking
Every bot action is automatically tracked with:
- **Success/Failure rate** - How often the bot completes tasks successfully
- **Execution time** - Average time to complete tasks
- **Error patterns** - Common failure reasons
- **Work log** - Detailed history of all actions

### 2. Feedback Collection
You can rate bot actions to help the system learn:

```python
# Via interactive command
CEO Bot > rate InboxBot email processing 5 "Excellent draft quality"

# Or programmatically
add_feedback("InboxBot", "Process inbox", 5, "Great job handling spam")
add_feedback("TeamsBot", "Reply to DM", 3, "Response was too formal")
```

Rating scale:
- 5 = Excellent
- 4 = Good
- 3 = Acceptable
- 2 = Needs improvement
- 1 = Poor

### 3. Automated Analysis

The CEO bot analyzes performance data:
- **Daily** at 6:00 PM - Performance analysis and suggestions
- **Weekly** on Mondays at 9:00 AM - Deep learning cycle with improvements

Analysis includes:
- Success rate trends
- Error frequency
- User satisfaction (from ratings)
- Performance bottlenecks
- Common failure patterns

### 4. AI-Generated Improvements

The LLM analyzes all data and suggests specific improvements like:
- Configuration adjustments
- Workflow optimizations
- Error handling improvements
- New feature additions

## Data Files

### work_log.json
Records all bot activities:
```json
[
  {
    "timestamp": "2026-02-25T10:30:00",
    "bot": "InboxBot",
    "task": "Process inbox",
    "status": "completed",
    "details": "Processed 15 emails, created 8 drafts"
  }
]
```

### feedback.json
Stores user ratings:
```json
[
  {
    "timestamp": "2026-02-25T11:00:00",
    "bot": "InboxBot",
    "action": "Draft email reply",
    "rating": 5,
    "comment": "Perfect response, very professional"
  }
]
```

### analytics.json
Performance metrics:
```json
{
  "InboxBot": {
    "success": 145,
    "failure": 5,
    "avg_time": 12.5
  }
}
```

### improvements.json
AI-generated suggestions:
```json
{
  "timestamp": "2026-02-25T18:00:00",
  "suggestions": [
    {
      "bot": "TeamsBot",
      "issue": "13% of responses are rated below 3",
      "suggestion": "Adjust reply_style to be less formal for internal DMs",
      "priority": "high"
    }
  ]
}
```

## Interactive Commands

### View Performance
```
CEO Bot > show bot performance
CEO Bot > analyze performance
```

### Generate Improvements
```
CEO Bot > generate improvement suggestions
CEO Bot > suggest improvements
```

### Provide Feedback
```
CEO Bot > rate InboxBot last action 5
CEO Bot > feedback TeamsBot response quality 4 "Good but could be faster"
```

### View Learning Data
```
# Check recent feedback
cat feedback.json

# View analytics
cat analytics.json

# See improvement suggestions
cat improvements.json
```

## Scheduled Learning Cycles

### Daily Performance Analysis (6:00 PM)
1. Collects day's performance data
2. Identifies issues and trends
3. Generates improvement suggestions
4. Logs findings to improvements.json

### Weekly Learning Cycle (Mondays 9:00 AM)
1. Comprehensive performance review
2. Analyzes week's feedback and errors
3. Generates detailed improvement report
4. Emails summary to rfoley@jeffcofibres.com
5. Suggests configuration changes

## Manual Learning Trigger

Force a learning cycle anytime:
```python
python -c "from CEO import weekly_learning_cycle; weekly_learning_cycle()"
```

Or via interactive mode:
```
CEO Bot > run learning cycle
CEO Bot > analyze all bots and suggest improvements
```

## Improvement Application

The CEO bot can automatically apply safe improvements:

```
CEO Bot > apply improvement from suggestions #1
```

Currently supported auto-improvements:
- Adjusting timeout values
- Modifying retry logic
- Updating filter keywords
- Changing schedule frequencies

**Note:** Critical changes (like model selection) require manual approval.

## Best Practices

### 1. Regular Feedback
Rate bot actions frequently, especially:
- When drafts are particularly good/bad
- After unusual errors
- When performance seems different

### 2. Review Weekly Reports
Check Monday morning emails for:
- Performance trends
- Recommended improvements
- Unusual patterns

### 3. Monitor Analytics
Periodically check analytics.json for:
- Declining success rates
- Increasing error counts
- Performance degradation

### 4. Test Improvements
Before applying suggestions:
1. Review the suggestion details
2. Test in a controlled way
3. Monitor impact for 24-48 hours
4. Revert if issues arise

## Customizing Learning Behavior

### Disable Learning
In CEO.py:
```python
LEARNING_ENABLED = False
```

### Change Analysis Frequency
Modify the schedule in `run_scheduled_tasks()`:
```python
# Daily at 2:00 PM instead of 6:00 PM
schedule.every().day.at("14:00").do(lambda: run_agent("Analyze bot performance"))

# Weekly on Fridays instead of Mondays
schedule.every().friday.at("09:00").do(weekly_learning_cycle)
```

### Adjust Feedback Weight
Modify how heavily recent feedback impacts analysis by changing the lookback window in `generate_improvement_suggestions()`:
```python
feedback = load_feedback()[-100:]  # Last 100 items (default is 50)
```

## Troubleshooting

### "No improvement suggestions generated"
- Not enough data collected yet (wait 24-48 hours)
- All bots performing well (no issues to fix)
- LLM connection issue (check Ollama)

### "Feedback not being recorded"
- Check file permissions on feedback.json
- Verify JSON format is valid
- Check ceo_bot.log for errors

### "Performance metrics are zero"
- Bots haven't run enough tasks yet
- Analytics file may be corrupted (delete and restart)
- Check that work logging is enabled

## Example Learning Session

```
CEO Bot > check bots
[All bots checked successfully]

CEO Bot > show performance
InboxBot: 95% success, 8 errors this week, avg rating 4.2/5
TeamsBot: 87% success, 15 errors this week, avg rating 3.8/5
...

CEO Bot > generate improvements
[AI analyzes performance...]

Generated 4 improvement suggestions:
1. [HIGH] TeamsBot - Increase unread detection timeout to reduce false negatives
2. [MEDIUM] InboxBot - Add "newsletter" to noise keywords
3. [LOW] SQLBot - Cache common queries for faster response
4. [MEDIUM] ChromeBot - Add retry logic for tray.io timeouts

CEO Bot > apply improvement #1
[Applying improvement to TeamsBot...]
Improvement applied successfully!

CEO Bot > rate TeamsBot detection improvement 5 "Much better unread detection now"
Feedback recorded.
```

## Integration with Morning Reports

Performance metrics are automatically included in daily morning emails:

```
Daily Work Summary - 2026-02-25
============================================

BOT PERFORMANCE (24 hours):
InboxBot: 24 tasks, 100% success, avg 4.5/5 rating
TeamsBot: 18 tasks, 94% success, avg 4.2/5 rating

RECENT ISSUES:
- TeamsBot: 1 connection timeout (auto-recovered)

PENDING IMPROVEMENTS:
- 2 high priority suggestions ready to apply
```

## Advanced: Custom Analysis

You can add custom analysis functions by extending `analyze_bot_performance()`:

```python
def analyze_custom_metric():
    work_log = load_work_log()

    # Your custom analysis
    response_times = [item["details"] for item in work_log if "response_time" in item]

    return {
        "avg_response_time": sum(response_times) / len(response_times),
        "trend": "improving" if is_improving(response_times) else "declining"
    }
```

Then add to the performance report in `weekly_learning_cycle()`.

## Privacy & Data Retention

- All learning data stays local (no cloud sync)
- Work logs rotate after 10,000 entries
- Feedback is kept indefinitely (for trend analysis)
- Analytics are cumulative (reset manually if needed)

To reset learning data:
```bash
rm feedback.json analytics.json improvements.json
# work_log.json will be preserved for morning reports
```
