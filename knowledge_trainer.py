#!/usr/bin/env python3
"""
Knowledge Base Trainer - Creates embeddings and trains bots

This script:
1. Loads the crawled knowledge base
2. Creates embeddings for semantic search
3. Builds training examples for common queries
4. Generates bot-specific context files
"""

import os
import json
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

import ollama

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_DIR = "knowledge_base"
TRAINING_DATA_DIR = "training_data"
os.makedirs(TRAINING_DATA_DIR, exist_ok=True)


class KnowledgeTrainer:
    """Trains bots using discovered knowledge."""

    def __init__(self):
        self.knowledge = self.load_knowledge_base()
        self.embeddings = {}
        self.training_examples = []

    def load_knowledge_base(self) -> Dict[str, Any]:
        """Load the latest knowledge base."""
        kb_file = os.path.join(KNOWLEDGE_BASE_DIR, "knowledge_base_latest.json")

        if not os.path.exists(kb_file):
            logger.error(f"Knowledge base not found at {kb_file}")
            logger.error("Please run knowledge_crawler.py first")
            return {}

        with open(kb_file, 'r') as f:
            return json.load(f)

    def train_all_bots(self) -> None:
        """Train all bots with discovered knowledge."""
        logger.info("Starting bot training...")

        self.train_sql_bot()
        self.generate_common_queries()
        self.create_context_files()
        self.generate_training_examples()

        logger.info("Bot training complete!")

    def train_sql_bot(self) -> None:
        """Create SQL bot training data from database schemas."""
        logger.info("Training SQL Bot...")

        sql_training = {
            "timestamp": datetime.now().isoformat(),
            "schemas": {},
            "example_queries": [],
            "table_relationships": [],
            "common_patterns": []
        }

        databases = self.knowledge.get("databases", {})

        for db_type, db_info in databases.items():
            logger.info(f"Processing {db_type}...")

            # Extract schema information
            if "schemas" in db_info:
                for schema_name, tables in db_info["schemas"].items():
                    sql_training["schemas"][f"{db_type}.{schema_name}"] = tables

            # Extract table info
            if "tables" in db_info:
                sql_training["table_list"] = db_info["tables"]

            # Extract relationships
            if "relationships" in db_info:
                sql_training["table_relationships"] = db_info["relationships"]

            # Generate example queries
            sql_training["example_queries"].extend(
                self.generate_sql_examples(db_info)
            )

        # Save SQL bot training data
        output_file = os.path.join(TRAINING_DATA_DIR, "sql_bot_training.json")
        with open(output_file, 'w') as f:
            json.dump(sql_training, f, indent=2)

        logger.info(f"SQL Bot training data saved to {output_file}")

        # Create quick reference for SQL bot
        self.create_sql_quick_reference(sql_training)

    def generate_sql_examples(self, db_info: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate example SQL queries from schema."""
        examples = []

        # Extract tables
        tables = []
        if "schemas" in db_info:
            for schema, schema_tables in db_info["schemas"].items():
                for table in schema_tables:
                    tables.append({
                        "schema": schema,
                        "name": table["name"],
                        "columns": table.get("columns", [])
                    })
        elif "tables" in db_info:
            # For databases that don't use schemas
            tables = db_info.get("columns", {})

        # Generate basic SELECT examples
        for table in tables[:20]:  # Limit to 20 tables
            table_name = f"{table.get('schema', '')}.{table['name']}" if 'schema' in table else table['name']
            columns = table.get('columns', [])

            if columns:
                col_names = ', '.join([col['name'] for col in columns[:5]])

                examples.append({
                    "question": f"Show me all data from {table['name']}",
                    "sql": f"SELECT {col_names} FROM {table_name}",
                    "table": table_name,
                    "type": "SELECT"
                })

                # Add WHERE example with first column
                if columns:
                    first_col = columns[0]['name']
                    examples.append({
                        "question": f"Find records in {table['name']} where {first_col} equals X",
                        "sql": f"SELECT * FROM {table_name} WHERE {first_col} = ?",
                        "table": table_name,
                        "type": "SELECT_WHERE"
                    })

                # Add COUNT example
                examples.append({
                    "question": f"How many records are in {table['name']}?",
                    "sql": f"SELECT COUNT(*) FROM {table_name}",
                    "table": table_name,
                    "type": "COUNT"
                })

        # Generate JOIN examples from relationships
        relationships = db_info.get("relationships", [])
        for rel in relationships[:10]:  # Limit to 10 joins
            examples.append({
                "question": f"Join {rel['parent_table']} with {rel['referenced_table']}",
                "sql": f"""SELECT *
FROM {rel['parent_table']} p
INNER JOIN {rel['referenced_table']} r ON p.{rel['parent_column']} = r.{rel['referenced_column']}""",
                "table": f"{rel['parent_table']}, {rel['referenced_table']}",
                "type": "JOIN"
            })

        return examples

    def create_sql_quick_reference(self, sql_training: Dict[str, Any]) -> None:
        """Create a quick reference guide for SQL bot."""
        ref = []
        ref.append("=" * 60)
        ref.append("SQL BOT QUICK REFERENCE")
        ref.append("=" * 60)
        ref.append("")

        # List all tables
        ref.append("AVAILABLE TABLES:")
        for table in sql_training.get("table_list", [])[:50]:
            ref.append(f"  - {table}")
        ref.append("")

        # Show schema structure
        ref.append("SCHEMA STRUCTURE:")
        for schema_name, tables in list(sql_training.get("schemas", {}).items())[:10]:
            ref.append(f"\n  {schema_name}:")
            for table in tables[:5]:
                ref.append(f"    {table['name']}:")
                for col in table.get("columns", [])[:10]:
                    ref.append(f"      - {col['name']} ({col.get('type', 'unknown')})")
        ref.append("")

        # Show relationships
        ref.append("TABLE RELATIONSHIPS:")
        for rel in sql_training.get("table_relationships", [])[:20]:
            ref.append(f"  {rel['parent_table']}.{rel['parent_column']} -> "
                      f"{rel['referenced_table']}.{rel['referenced_column']}")
        ref.append("")

        # Show example queries
        ref.append("EXAMPLE QUERIES:")
        for example in sql_training.get("example_queries", [])[:15]:
            ref.append(f"\n  Q: {example['question']}")
            ref.append(f"  SQL: {example['sql']}")
        ref.append("")

        ref.append("=" * 60)

        ref_text = "\n".join(ref)

        # Save quick reference
        ref_file = os.path.join(TRAINING_DATA_DIR, "sql_quick_reference.txt")
        with open(ref_file, 'w') as f:
            f.write(ref_text)

        print(ref_text)
        logger.info(f"SQL quick reference saved to {ref_file}")

    def generate_common_queries(self) -> None:
        """Generate common query patterns using LLM."""
        logger.info("Generating common query patterns with LLM...")

        # Build context from knowledge base
        context = self.build_llm_context()

        prompt = f"""Based on this infrastructure information, generate 20 common support questions that users might ask:

{context}

For each question, provide:
1. The question
2. Which bot should handle it (SQLBot, InboxBot, TeamsBot, ChromeBot)
3. Key information needed to answer it

Format as JSON array: [{{"question": "...", "bot": "...", "context": "..."}}]"""

        try:
            response = ollama.chat(
                model="llama3.1",
                messages=[
                    {"role": "system", "content": "You are a system analyst. Generate realistic support questions."},
                    {"role": "user", "content": prompt}
                ]
            )

            content = response["message"]["content"]

            # Try to extract JSON
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            common_queries = json.loads(content)

            # Save common queries
            output_file = os.path.join(TRAINING_DATA_DIR, "common_queries.json")
            with open(output_file, 'w') as f:
                json.dump(common_queries, f, indent=2)

            logger.info(f"Generated {len(common_queries)} common queries")

        except Exception as e:
            logger.error(f"Error generating common queries: {e}")

    def build_llm_context(self) -> str:
        """Build summarized context for LLM prompts."""
        context = []

        # Database info
        db_info = self.knowledge.get("databases", {})
        if db_info:
            context.append("DATABASES:")
            for db_type, db in db_info.items():
                context.append(f"  {db_type}: {len(db.get('tables', []))} tables")
                context.append(f"    Tables: {', '.join(db.get('tables', [])[:10])}")

        # System info
        sys_info = self.knowledge.get("infrastructure", {}).get("local_system", {})
        if sys_info:
            context.append(f"\nSYSTEM: {sys_info.get('hostname', 'Unknown')}")
            context.append(f"  Platform: {sys_info.get('platform', 'Unknown')}")

        # Applications
        apps = self.knowledge.get("applications", {})
        if apps.get("python_packages"):
            context.append(f"\nPython packages: {len(apps['python_packages'])}")

        return "\n".join(context)

    def create_context_files(self) -> None:
        """Create bot-specific context files for RAG."""
        logger.info("Creating bot context files...")

        # SQL Bot context
        sql_context = {
            "bot_name": "SQLBot",
            "capabilities": [
                "Execute SQL queries",
                "Answer database questions",
                "Provide table information",
                "Generate reports"
            ],
            "knowledge": {
                "databases": self.knowledge.get("databases", {}),
                "common_patterns": self.get_sql_patterns()
            }
        }

        sql_context_file = os.path.join(TRAINING_DATA_DIR, "sql_bot_context.json")
        with open(sql_context_file, 'w') as f:
            json.dump(sql_context, f, indent=2, default=str)

        # Create contexts for other bots
        for bot_name in ["InboxBot", "TeamsBot", "ChromeBot"]:
            bot_context = {
                "bot_name": bot_name,
                "infrastructure": self.knowledge.get("infrastructure", {}),
                "system_info": self.knowledge.get("infrastructure", {}).get("local_system", {})
            }

            context_file = os.path.join(TRAINING_DATA_DIR, f"{bot_name.lower()}_context.json")
            with open(context_file, 'w') as f:
                json.dump(bot_context, f, indent=2, default=str)

        logger.info("Bot context files created")

    def get_sql_patterns(self) -> List[str]:
        """Extract common SQL patterns from schema."""
        patterns = []

        db_info = self.knowledge.get("databases", {})
        for db_type, db in db_info.items():
            # Extract table patterns
            tables = db.get("tables", [])
            if tables:
                patterns.append(f"Tables available: {', '.join(tables[:20])}")

            # Extract relationship patterns
            rels = db.get("relationships", [])
            if rels:
                patterns.append("Common joins:")
                for rel in rels[:5]:
                    patterns.append(f"  {rel['parent_table']} -> {rel['referenced_table']}")

        return patterns

    def generate_training_examples(self) -> None:
        """Generate training examples using LLM."""
        logger.info("Generating training examples...")

        training_examples = {
            "sql_queries": [],
            "troubleshooting": [],
            "documentation": []
        }

        # Generate SQL training examples
        db_info = self.knowledge.get("databases", {})
        if db_info:
            for db_type, db in db_info.items():
                for table in db.get("tables", [])[:10]:
                    training_examples["sql_queries"].append({
                        "input": f"Show me data from {table}",
                        "expected_action": "Generate SELECT query",
                        "table": table,
                        "bot": "SQLBot"
                    })

        # Save training examples
        output_file = os.path.join(TRAINING_DATA_DIR, "training_examples.json")
        with open(output_file, 'w') as f:
            json.dump(training_examples, f, indent=2)

        logger.info(f"Training examples saved to {output_file}")


def main():
    """Run the knowledge trainer."""
    print("Starting Knowledge Base Trainer...")
    print("")

    trainer = KnowledgeTrainer()

    if not trainer.knowledge:
        print("ERROR: No knowledge base found!")
        print("Please run knowledge_crawler.py first.")
        return

    trainer.train_all_bots()

    print("\nTraining complete!")
    print(f"Training data saved to: {TRAINING_DATA_DIR}/")
    print("")
    print("Files created:")
    print("  - sql_bot_training.json       (SQL schema and examples)")
    print("  - sql_quick_reference.txt     (Human-readable reference)")
    print("  - common_queries.json         (Expected user questions)")
    print("  - *_context.json              (Bot-specific contexts)")
    print("  - training_examples.json      (Training examples)")
    print("")
    print("Next: Update CEO.py to use these context files!")


if __name__ == "__main__":
    main()
