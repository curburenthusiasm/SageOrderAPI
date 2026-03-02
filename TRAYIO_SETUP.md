# Tray.io Knowledge Graph Setup

## Overview

The Tray.io crawler builds a knowledge graph of your EDI workflows, enabling ChromeBot to intelligently manage and answer questions about your integrations.

## Quick Start

### Option 1: Automatic Crawl (with API access)

```bash
# 1. Add Tray.io credentials to .env
echo "TRAY_IO_API_TOKEN=your_api_token_here" >> .env

# 2. Run the crawler
python trayio_crawler.py

# 3. Review what was discovered
cat knowledge_base/trayio/trayio_summary.txt
```

### Option 2: Manual Configuration (no API access)

```bash
# 1. Run crawler to generate template
python trayio_crawler.py

# 2. Edit the generated template
# File: knowledge_base/trayio/workflows_template.json

# 3. Fill in your workflows
# Add your 850, 810, 856 workflows, etc.
```

### Option 3: Selenium UI Crawl

```bash
# 1. Install Selenium
pip install selenium

# 2. Add credentials to .env
echo "TRAY_IO_USERNAME=your_email@jeffcofibres.com" >> .env
echo "TRAY_IO_PASSWORD=your_password" >> .env

# 3. Run crawler
python trayio_crawler.py
```

## What Gets Discovered

### Workflows
```json
{
  "name": "850 Purchase Order Processing",
  "description": "Receives 850 EDI and creates orders",
  "status": "enabled",
  "trigger_type": "webhook",
  "steps": [
    "Receive EDI file",
    "Parse EDI",
    "Create order in ERP"
  ],
  "edi_related": true
}
```

### EDI Patterns
- **Inbound Orders** (850)
- **Outbound Invoices** (810)
- **Shipment Notifications** (856)
- **Inventory Updates** (846)
- **Acknowledgments** (997)

### Connectors
- HTTP/Webhook
- SFTP
- Database
- Email
- Custom APIs

## Using the Knowledge Graph

### Ask ChromeBot Questions

```bash
python CEO.py

# Check workflows
CEO Bot > check tray.io workflows
[Shows: 12 workflows, 8 EDI-related]

# Trigger workflow
CEO Bot > trigger workflow "850 Purchase Order Processing"
[Triggers the workflow]

# Ask questions
CEO Bot > what EDI workflows do we have?
CEO Bot > how do I process 810 invoices?
CEO Bot > which workflows handle shipment notifications?
```

### Example Responses

**Question:** "What EDI workflows do we have?"

**ChromeBot Response:**
```
We have 8 EDI-related workflows:

Inbound Orders:
  - 850 Purchase Order Processing (enabled)
  - 875 Grocery Order Processing (enabled)

Outbound Invoices:
  - 810 Invoice Generation (enabled)
  - 810 Credit Memo Generation (enabled)

Shipment Notifications:
  - 856 ASN Generation (enabled)

The 850 workflow runs on-demand via webhook,
while 810 and 856 run on daily schedules.
```

## Manual Template Example

If API access isn't available, edit `knowledge_base/trayio/workflows_template.json`:

```json
{
  "workflows": [
    {
      "name": "850 Purchase Order Inbound",
      "description": "Receives 850 EDI files from customers via SFTP, parses them, and creates orders in our ERP system",
      "trigger_type": "schedule",
      "schedule": "every 15 minutes",
      "status": "enabled",
      "edi_type": "850 - Purchase Order",
      "steps": [
        "Connect to SFTP server",
        "Download new 850 files",
        "Parse EDI format",
        "Validate customer and items",
        "Create order in ERP",
        "Move file to processed folder",
        "Send 997 acknowledgment"
      ]
    },
    {
      "name": "810 Invoice Outbound",
      "description": "Generates 810 EDI invoices for shipped orders and sends to customers",
      "trigger_type": "schedule",
      "schedule": "daily at 6:00 PM",
      "status": "enabled",
      "edi_type": "810 - Invoice",
      "steps": [
        "Query shipped orders from ERP",
        "Format as 810 EDI",
        "Upload to customer SFTP",
        "Mark orders as invoiced",
        "Log transaction"
      ]
    }
  ]
}
```

## Configuration (.env)

```bash
# Option 1: API Token (preferred)
TRAY_IO_API_TOKEN=your_api_token_here
TRAY_IO_URL=https://app.tray.io

# Option 2: Username/Password (for Selenium)
TRAY_IO_USERNAME=your_email@jeffcofibres.com
TRAY_IO_PASSWORD=your_password
TRAY_IO_URL=https://app.tray.io
```

## Integration with CEO Bot

The ChromeBot automatically loads the knowledge graph at startup:

```python
# In CEO.py
class ChromeBot:
    def __init__(self):
        self.knowledge = self.load_knowledge_graph()
        # Automatically loads: knowledge_base/trayio/trayio_knowledge_latest.json
```

## Re-Crawl After Changes

```bash
# When you add/modify workflows in Tray.io
python trayio_crawler.py

# Restart CEO bot to reload knowledge
# (or it will auto-reload if you configured hot-reload)
```

## Use Cases

### 1. Workflow Status Monitoring

```
CEO Bot > check all EDI workflows
→ Shows status of all 850, 810, 856 workflows
```

### 2. Manual Workflow Triggers

```
CEO Bot > trigger 810 invoice workflow
→ Manually runs the invoice generation
```

### 3. Troubleshooting Support

```
User: "The 850 orders aren't processing"
CEO Bot (to user): "I'll check the 850 workflow status"
CEO Bot > check workflow "850 Purchase Order Processing"
→ Shows last run time, status, errors
```

### 4. Documentation

```
CEO Bot > explain how 856 shipments work
→ ChromeBot uses knowledge graph to explain the ASN workflow
```

## Advanced: API Integration

To enable real API calls (not just knowledge graph lookups):

```python
# In CEO.py, update ChromeBot.trigger_edi_sync()

import requests

def trigger_edi_sync(self, workflow_name: str):
    workflow = self.get_workflow_by_name(workflow_name)
    workflow_id = workflow.get("id")

    # Call Tray.io API
    headers = {"Authorization": f"Bearer {os.getenv('TRAY_IO_API_TOKEN')}"}
    response = requests.post(
        f"https://app.tray.io/api/v1/workflows/{workflow_id}/trigger",
        headers=headers
    )

    return {"ok": response.status_code == 200}
```

## Files Created

```
knowledge_base/trayio/
├── trayio_knowledge_latest.json   # Current knowledge graph
├── trayio_knowledge_20260226.json # Timestamped backup
├── trayio_summary.txt             # Human-readable summary
└── workflows_template.json        # Template for manual config
```

## Troubleshooting

### "No workflows discovered"

1. Check Tray.io credentials in .env
2. Try manual configuration option
3. Check trayio_crawler.log for errors

### "Workflow not found"

1. Re-run crawler: `python trayio_crawler.py`
2. Check workflow name matches exactly
3. ChromeBot will suggest similar names

### "API authentication failed"

1. Verify TRAY_IO_API_TOKEN is correct
2. Check token hasn't expired
3. Try username/password with Selenium instead

---

**Now your ChromeBot knows your EDI workflows!** 🚀

It can check status, trigger workflows, and answer questions about your integrations.
