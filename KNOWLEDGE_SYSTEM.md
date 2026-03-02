# Knowledge Base System - Bot Training Guide

## Overview

The Knowledge Base System crawls your infrastructure and trains the bots with deep context about your systems, databases, and architecture. This enables bots to answer ticketing questions accurately without manual configuration.

## How It Works

```
1. CRAWL → Discover infrastructure (schemas, servers, apps, docs)
2. TRAIN → Build knowledge base with embeddings & examples
3. DEPLOY → Bots use RAG to answer questions with context
4. IMPROVE → Continuous learning from queries & feedback
```

## Quick Start

### Step 1: Crawl Your Infrastructure

```bash
python knowledge_crawler.py
```

**This will discover:**
- ✅ Database schemas (SQL Server, MySQL, PostgreSQL)
- ✅ Table structures, columns, relationships, foreign keys
- ✅ Stored procedures and views
- ✅ Server information (hostname, platform, network)
- ✅ Installed applications and services
- ✅ Configuration files
- ✅ Documentation files (README, setup guides)

**Output:**
- `knowledge_base/knowledge_base_latest.json` - Complete discovery
- `knowledge_base/summary.txt` - Human-readable summary

### Step 2: Train the Bots

```bash
python knowledge_trainer.py
```

**This will create:**
- ✅ SQL query examples from your schema
- ✅ Table relationship mappings
- ✅ Common query patterns
- ✅ Bot-specific context files
- ✅ Training examples

**Output:**
- `training_data/sql_bot_training.json` - Full training data
- `training_data/sql_quick_reference.txt` - Quick reference guide
- `training_data/common_queries.json` - Expected questions
- `training_data/*_context.json` - Bot contexts

### Step 3: Use the Knowledge

The CEO Bot automatically loads this knowledge! Just ask questions:

```
CEO Bot > answer: how many orders were placed today?

[SQLBot uses knowledge base to build context...]
[Generates SQL: SELECT COUNT(*) FROM orders WHERE order_date = CAST(GETDATE() AS DATE)]
[Executes query and returns result]

Result: 47 orders placed today
```

## What Gets Learned

### Database Knowledge

**Schema Discovery:**
```json
{
  "tables": ["customers", "orders", "products", "inventory"],
  "schemas": {
    "dbo.customers": {
      "columns": [
        {"name": "customer_id", "type": "int", "nullable": false},
        {"name": "customer_name", "type": "varchar(100)", "nullable": false},
        {"name": "email", "type": "varchar(255)", "nullable": true}
      ]
    }
  },
  "relationships": [
    {
      "parent_table": "orders",
      "parent_column": "customer_id",
      "referenced_table": "customers",
      "referenced_column": "customer_id"
    }
  ]
}
```

**Generated Examples:**
- "Show me all customers" → `SELECT * FROM dbo.customers`
- "How many orders?" → `SELECT COUNT(*) FROM dbo.orders`
- "Join orders with customers" → `SELECT * FROM orders o JOIN customers c ON o.customer_id = c.customer_id`

### System Architecture

```json
{
  "infrastructure": {
    "hostname": "SERVER01",
    "platform": "Windows Server 2019",
    "ip_address": "192.168.1.100",
    "applications": ["IIS", "SQL Server", "Python 3.11"]
  }
}
```

### Documentation

Extracts content from:
- README.md files
- Setup guides
- Architecture docs
- API documentation
- Configuration examples

## RAG (Retrieval-Augmented Generation)

### How SQL Bot Uses RAG

When you ask: **"Show me customers who placed orders in January"**

1. **Retrieve** relevant context from knowledge base:
   ```
   - customers table has: customer_id, customer_name, email
   - orders table has: order_id, customer_id, order_date
   - Relationship: orders.customer_id → customers.customer_id
   ```

2. **Augment** the LLM prompt with this context:
   ```
   System: You have access to these tables:
   - customers (customer_id, customer_name, email)
   - orders (order_id, customer_id, order_date)

   Relationship: orders.customer_id → customers.customer_id

   User: Show me customers who placed orders in January
   ```

3. **Generate** accurate SQL:
   ```sql
   SELECT DISTINCT c.customer_name, c.email
   FROM customers c
   INNER JOIN orders o ON c.customer_id = o.customer_id
   WHERE MONTH(o.order_date) = 1
     AND YEAR(o.order_date) = YEAR(GETDATE())
   ```

## Configuration

### Database Credentials (.env)

```bash
# Primary database
DB_HOST=your-sql-server.database.windows.net
DB_USER=readonly_user
DB_PASSWORD=secure_password
DB_NAME=production_db
```

**Security Best Practices:**
- Use read-only accounts
- Limit access to necessary tables
- Use VPN/firewall restrictions
- Never commit .env file

### Crawler Settings

Edit `knowledge_crawler.py` to customize:

```python
# Limit depth of file system crawl
if root.count(os.sep) - search_dir.count(os.sep) > 2:
    break  # Only go 2 directories deep

# Skip certain directories
dirs[:] = [d for d in dirs if d not in ['node_modules', '__pycache__', 'venv']]

# Limit results
found_configs[:100]  # Only keep first 100 config files
```

### Trainer Settings

Edit `knowledge_trainer.py` to customize:

```python
# Number of tables to process
for table in tables[:20]:  # Process first 20 tables

# Number of example queries per table
examples.append({...})  # Add more patterns

# Context size for LLM
context_parts.append(", ".join(self.training_data["table_list"][:30]))  # Top 30 tables
```

## Advanced Features

### Multi-Database Support

The crawler automatically detects:
- SQL Server (via pyodbc)
- MySQL (via pymysql)
- PostgreSQL (via psycopg2)

Install drivers as needed:
```bash
pip install pyodbc pymysql psycopg2-binary
```

### Incremental Updates

Re-run the crawler to update knowledge:

```bash
# Full re-crawl
python knowledge_crawler.py

# The latest knowledge base is automatically used
```

Old versions are preserved:
```
knowledge_base/
├── knowledge_base_20260225_100000.json
├── knowledge_base_20260226_100000.json  # Yesterday
└── knowledge_base_latest.json            # Current (symlink)
```

### Custom Training Examples

Add your own examples in `knowledge_trainer.py`:

```python
def add_custom_examples(self):
    """Add domain-specific examples."""
    custom = [
        {
            "question": "Show sales by region",
            "sql": "SELECT region, SUM(sales) FROM sales_data GROUP BY region",
            "table": "sales_data",
            "type": "AGGREGATE"
        },
        {
            "question": "Top 10 customers by revenue",
            "sql": "SELECT TOP 10 customer_name, SUM(order_total) as revenue FROM orders JOIN customers ON orders.customer_id = customers.id GROUP BY customer_name ORDER BY revenue DESC",
            "table": "orders, customers",
            "type": "TOP_N"
        }
    ]

    return custom
```

## Use Cases

### 1. Ticketing System Integration

**Ticket:** "How many open tickets are assigned to John?"

```
CEO Bot > process ticket: How many open tickets are assigned to John?

[SQLBot loads context: tickets table with assigned_to, status columns]
[Generates: SELECT COUNT(*) FROM tickets WHERE assigned_to = 'John' AND status = 'Open']
[Returns: 12 open tickets]
```

### 2. Business Intelligence

**Query:** "What was our revenue last quarter?"

```
[SQLBot knows: sales table, order_date, amount columns]
[Generates: SELECT SUM(amount) FROM sales WHERE order_date >= DATEADD(quarter, -1, GETDATE())]
[Returns: $487,234.56]
```

### 3. Data Audits

**Task:** "Find customers with no orders in the past year"

```
[SQLBot knows: customers and orders tables, relationship]
[Generates complex NOT EXISTS or LEFT JOIN query]
[Returns: List of 47 inactive customers]
```

### 4. System Health

**Question:** "Show me failed login attempts today"

```
[SQLBot knows: auth_logs table structure]
[Generates: SELECT * FROM auth_logs WHERE status = 'failed' AND log_date = CAST(GETDATE() AS DATE)]
[Returns: 3 failed attempts]
```

## Monitoring Knowledge Quality

### Check Coverage

```python
# View what was discovered
cat knowledge_base/summary.txt

# Check SQL training data
cat training_data/sql_quick_reference.txt

# See example queries
jq '.example_queries' training_data/sql_bot_training.json
```

### Test Query Generation

```bash
# Interactive testing
python CEO.py

CEO Bot > test sql: how many users signed up today?
[Shows generated SQL and result]

CEO Bot > test sql: show me top 5 products by sales
[Shows generated SQL and result]
```

### Validate Accuracy

```python
# Check if bot knows your tables
CEO Bot > what tables are available?

# Check if relationships are understood
CEO Bot > how do I join orders with customers?

# Test complex queries
CEO Bot > generate SQL to find customers who haven't ordered in 90 days
```

## Continuous Improvement

### 1. Re-Crawl After Schema Changes

```bash
# After adding new tables or columns
python knowledge_crawler.py
python knowledge_trainer.py

# Restart CEO bot to load new knowledge
```

### 2. Add Feedback

```bash
CEO Bot > rate SQLBot last query 5 "Perfect SQL generation"
CEO Bot > rate SQLBot last query 2 "Wrong table used, should use orders_archive"
```

### 3. Review Generated Queries

```bash
# Check work log for SQL quality
cat work_log.json | jq '.[] | select(.bot=="SQLBot")'

# Identify common errors
grep "failed" work_log.json | grep SQLBot
```

### 4. Enhance Training Data

Add corrected examples to `training_data/sql_bot_training.json`:

```json
{
  "example_queries": [
    {
      "question": "Show archived orders from last year",
      "sql": "SELECT * FROM orders_archive WHERE YEAR(order_date) = YEAR(GETDATE()) - 1",
      "table": "orders_archive",
      "type": "HISTORICAL",
      "notes": "Use orders_archive for old data, not orders table"
    }
  ]
}
```

## Troubleshooting

### "No knowledge base found"

Run the crawler first:
```bash
python knowledge_crawler.py
```

### "Could not connect to database"

Check credentials in .env:
```bash
cat .env | grep DB_
```

Test connection:
```python
python -c "import pyodbc; pyodbc.connect('...')"
```

### "Generated SQL is incorrect"

1. Check if schema is current: `python knowledge_crawler.py`
2. Add training examples for this query pattern
3. Provide feedback: `CEO Bot > rate SQLBot query 2 "wrong columns"`
4. Check if table names changed

### "Bot doesn't know about new tables"

```bash
# Re-crawl to discover new tables
python knowledge_crawler.py

# Re-train to generate new examples
python knowledge_trainer.py

# Restart CEO bot
```

## Performance Tips

### Limit Crawl Scope

```python
# In knowledge_crawler.py
search_dirs = [
    r"C:\Specific\Project\Folder",  # Only this folder
]

# Skip large directories
if any(skip in root for skip in ['node_modules', 'venv', 'backup']):
    continue
```

### Cache Knowledge

The knowledge base is loaded once at startup. For large schemas:

```python
# Limit tables in context
context_parts.append(", ".join(self.training_data["table_list"][:20]))  # Top 20 only
```

### Optimize Query Generation

```python
# Use faster models for simple queries
if is_simple_query(question):
    model = "llama3.1:8b"  # Faster
else:
    model = "llama3.1:70b"  # More accurate
```

## Security Considerations

### Database Access

- ✅ Use read-only accounts
- ✅ Limit to specific schemas
- ✅ Audit all queries
- ✅ Set query timeouts
- ✅ Block destructive operations

### Knowledge Base Protection

- ✅ Don't expose schemas publicly
- ✅ Encrypt sensitive data
- ✅ Restrict file permissions
- ✅ .gitignore knowledge_base/
- ✅ Regular backups

### Query Safety

Built-in protections:
```python
# Destructive query blocker
if any(keyword in query.upper() for keyword in ['DROP', 'DELETE', 'TRUNCATE']):
    return {"error": "Destructive queries blocked"}

# Timeout protection
conn = pyodbc.connect(conn_str, timeout=10)

# Row limit
cursor.execute(f"{query} LIMIT 1000")  # Max 1000 rows
```

## Next Steps

1. ✅ Run `python knowledge_crawler.py`
2. ✅ Run `python knowledge_trainer.py`
3. ✅ Test with `python CEO.py`
4. ✅ Ask SQL questions and verify accuracy
5. ✅ Provide feedback to improve
6. ✅ Re-crawl after schema changes
7. ✅ Monitor query quality in work_log.json

---

**Your bots are now intelligent!** They understand your infrastructure and can answer questions accurately using the discovered knowledge.
