# Bot Training Quick Start Guide

## 🎯 Goal

Train your bots to understand your infrastructure so they can answer ticketing questions like:

- "How many orders were placed today?"
- "Show me customers who haven't ordered in 90 days"
- "What's the status of ticket #12345?"
- "Find all users created this month"

Without you having to manually teach them about your database schema!

## 🚀 One-Shot Training

### Run This Single Command:

```bash
python train_bots.py
```

That's it! This script will:

1. ✅ **Crawl** your infrastructure
   - Discover all database schemas (tables, columns, relationships)
   - Find servers, applications, and configurations
   - Extract documentation from README files

2. ✅ **Train** the SQL Bot
   - Generate example queries for your tables
   - Build relationship mappings
   - Create context for RAG (Retrieval-Augmented Generation)

3. ✅ **Validate** the knowledge
   - Check that schemas were discovered
   - Verify training data was created
   - Test that everything is ready

4. ✅ **Generate** documentation
   - SQL quick reference guide
   - Common query examples
   - Bot capabilities summary

## 📋 Prerequisites

### 1. Database Access

Add credentials to `.env`:

```bash
DB_HOST=your-server.database.windows.net
DB_USER=readonly_user          # ← Use read-only account!
DB_PASSWORD=secure_password
DB_NAME=your_database
```

### 2. Python Packages

```bash
pip install ollama pywin32 pyodbc python-dotenv
```

Optional (for other databases):
```bash
pip install pymysql psycopg2-binary
```

### 3. Ollama Running

```bash
ollama list  # Check if Ollama is running
ollama pull llama3.1  # Ensure model is downloaded
```

## 📖 What Happens During Training

### Phase 1: Infrastructure Crawl (2-5 minutes)

```
🔍 Discovering databases...
   ✅ Connected to SQL Server
   ✅ Found 47 tables
   ✅ Discovered 23 foreign key relationships
   ✅ Found 8 stored procedures

🔍 Discovering system info...
   ✅ Hostname: SERVER01
   ✅ Platform: Windows Server 2019
   ✅ IP: 192.168.1.100

🔍 Extracting documentation...
   ✅ Found 3 README files
   ✅ Found 12 configuration files

💾 Knowledge base saved to: knowledge_base/knowledge_base_latest.json
```

### Phase 2: Bot Training (1-3 minutes)

```
🧠 Training SQL Bot...
   ✅ Generated 94 example queries
   ✅ Mapped table relationships
   ✅ Created SQL quick reference

🧠 Generating common queries...
   ✅ Generated 20 expected questions
   ✅ Created training examples

💾 Training data saved to: training_data/
```

### Phase 3: Validation

```
✅ Found 1 database(s)
   - SQL Server: 47 tables
✅ System info: SERVER01
✅ Found 3 documentation file(s)
✅ training_data/sql_bot_training.json
✅ training_data/sql_quick_reference.txt
✅ training_data/sql_bot_context.json

✅ All validation checks passed!
```

## 🎓 What the Bot Learns

### Example: You have this database

```
Tables:
├── customers (customer_id, customer_name, email, created_date)
├── orders (order_id, customer_id, order_date, total_amount)
└── products (product_id, product_name, price, inventory)

Relationships:
└── orders.customer_id → customers.customer_id
```

### The Bot Learns:

```json
{
  "Available tables": [
    "customers", "orders", "products"
  ],
  "Example queries": [
    {
      "question": "Show me all customers",
      "sql": "SELECT customer_id, customer_name, email FROM customers"
    },
    {
      "question": "How many orders today?",
      "sql": "SELECT COUNT(*) FROM orders WHERE order_date = CAST(GETDATE() AS DATE)"
    },
    {
      "question": "Join orders with customers",
      "sql": "SELECT c.customer_name, o.total_amount FROM customers c JOIN orders o ON c.customer_id = o.customer_id"
    }
  ]
}
```

### Now You Can Ask:

```
CEO Bot > answer: how many customers signed up this month?

[SQLBot uses knowledge base]
[Generates: SELECT COUNT(*) FROM customers WHERE MONTH(created_date) = MONTH(GETDATE())]
[Executes and returns: 147 customers]
```

## 🔬 Testing the Training

### 1. Check What Was Discovered

```bash
# View summary
cat knowledge_base/summary.txt

# Check SQL reference
cat training_data/sql_quick_reference.txt
```

### 2. Interactive Testing

```bash
python CEO.py

CEO Bot > what tables are available?
CEO Bot > answer: count records in customers table
CEO Bot > answer: show me top 5 products by price
```

### 3. Validate Queries

The bot will:
- ✅ Use correct table names from your schema
- ✅ Use correct column names
- ✅ Apply correct JOINs based on relationships
- ✅ Follow SQL best practices

## 🔄 Re-Training

### When to Re-Train

Re-run training when:
- ✅ Database schema changes (new tables, columns)
- ✅ New servers or applications added
- ✅ Documentation updated
- ✅ Bot gives incorrect answers (outdated knowledge)

### How to Re-Train

```bash
# Quick re-train (uses same settings)
python train_bots.py

# Or manual steps:
python knowledge_crawler.py  # Re-discover infrastructure
python knowledge_trainer.py  # Re-generate training data
```

### Incremental Updates

Old knowledge bases are preserved:

```
knowledge_base/
├── knowledge_base_20260225_100000.json  # Previous
├── knowledge_base_20260226_100000.json  # Yesterday
└── knowledge_base_latest.json            # Current ← Always used
```

## 🎯 Use Cases

### Ticketing System Integration

```
User ticket: "How many open tickets are assigned to me?"

CEO Bot detects this is a SQL question
   → Delegates to SQLBot
   → SQLBot uses knowledge base
   → Knows: tickets table, assigned_to column, status column
   → Generates: SELECT COUNT(*) FROM tickets WHERE assigned_to = 'rfoley' AND status = 'Open'
   → Executes and returns: "You have 7 open tickets"
```

### Business Intelligence

```
Manager asks: "What was our revenue last quarter?"

SQLBot:
   → Knows: sales table, order_date, amount columns
   → Generates: SELECT SUM(amount) FROM sales WHERE order_date >= DATEADD(quarter, -1, GETDATE())
   → Returns: "$487,234.56"
```

### Data Audits

```
Compliance team: "Find customers with no orders in past year"

SQLBot:
   → Knows: customers and orders tables + relationship
   → Generates: SELECT c.* FROM customers c LEFT JOIN orders o ON c.customer_id = o.customer_id AND o.order_date > DATEADD(year, -1, GETDATE()) WHERE o.order_id IS NULL
   → Returns: 47 inactive customers
```

## 🛠 Troubleshooting

### "No databases discovered"

**Problem:** Crawler couldn't connect to database

**Solutions:**
1. Check `.env` has correct DB_HOST, DB_USER, DB_PASSWORD
2. Test connection: `python -c "import pyodbc; pyodbc.connect('...')"`
3. Verify network access (VPN, firewall)
4. Check database is running
5. Try: `pip install pyodbc --upgrade`

### "Generated SQL is wrong"

**Problem:** Bot uses incorrect table/column names

**Solutions:**
1. Re-run crawler: `python knowledge_crawler.py`
2. Check if schema changed in database
3. View what bot knows: `cat training_data/sql_quick_reference.txt`
4. Add custom examples to `knowledge_trainer.py`

### "Training failed"

**Problem:** Error during training process

**Solutions:**
1. Check logs: `cat knowledge_crawler.log`
2. Ensure Ollama is running: `ollama list`
3. Check disk space (knowledge base can be large)
4. Try with `DEBUG=True` in scripts

### "Bot doesn't know about new table"

**Problem:** Added table after initial training

**Solutions:**
1. Re-run training: `python train_bots.py`
2. Restart CEO bot to reload knowledge
3. Test: `CEO Bot > what tables are available?`

## 📊 Knowledge Base Files

After training, you'll have:

```
MorningTaskBot/
├── knowledge_base/
│   ├── knowledge_base_latest.json     ← Complete infrastructure discovery
│   └── summary.txt                    ← Human-readable summary
│
├── training_data/
│   ├── sql_bot_training.json          ← Full training data
│   ├── sql_quick_reference.txt        ← Quick reference guide
│   ├── sql_bot_context.json           ← RAG context for SQL Bot
│   ├── common_queries.json            ← Expected questions
│   └── training_examples.json         ← Training examples
│
└── *.log files                         ← Debug logs
```

### File Sizes (Approximate)

- Small schema (10 tables): ~100KB
- Medium schema (50 tables): ~500KB
- Large schema (200 tables): ~2MB
- Documentation included: +1-5MB

## 🔐 Security

### Database Access

✅ **DO:**
- Use read-only database accounts
- Limit to specific schemas if possible
- Use VPN/firewall restrictions
- Keep .env file secure (never commit to git)

❌ **DON'T:**
- Use admin/write accounts
- Expose credentials in code
- Store passwords in plain text outside .env
- Give bots DELETE/UPDATE permissions

### Knowledge Base Protection

✅ **DO:**
- Keep knowledge_base/ in .gitignore
- Restrict file permissions
- Backup regularly
- Audit what's discovered

❌ **DON'T:**
- Commit to public repositories
- Share knowledge base files externally
- Include in documentation
- Expose via web endpoints

## 📈 Next Steps

1. ✅ **Run training:**
   ```bash
   python train_bots.py
   ```

2. ✅ **Start CEO Bot:**
   ```bash
   python CEO.py
   ```

3. ✅ **Test with questions:**
   ```
   CEO Bot > answer: how many users in the system?
   CEO Bot > answer: show top 10 orders by amount
   CEO Bot > answer: find inactive customers
   ```

4. ✅ **Provide feedback:**
   ```
   CEO Bot > rate SQLBot last query 5 "Perfect!"
   CEO Bot > rate SQLBot last query 3 "Wrong table used"
   ```

5. ✅ **Monitor quality:**
   ```bash
   cat work_log.json | grep SQLBot
   ```

6. ✅ **Re-train periodically:**
   ```bash
   # Monthly or after schema changes
   python train_bots.py
   ```

---

**🎉 Your bots are now intelligent!**

They understand your infrastructure and can answer questions accurately without manual configuration. The more you use them and provide feedback, the better they get!
