#!/usr/bin/env python3
"""
Knowledge Base Crawler - Discovers and documents server infrastructure

This script crawls your infrastructure to build a comprehensive knowledge base:
- Database schemas and relationships
- Server configurations and network topology
- Application architectures
- Common queries and patterns
- Business logic and workflows

The knowledge base is then used by bots for context-aware responses.
"""

import os
import json
import time
import socket
import platform
from datetime import datetime
from typing import Any, Dict, List, Optional
import subprocess
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('knowledge_crawler.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Knowledge base storage
KNOWLEDGE_BASE_DIR = "knowledge_base"
os.makedirs(KNOWLEDGE_BASE_DIR, exist_ok=True)


class KnowledgeCrawler:
    """Crawls infrastructure and builds knowledge base."""

    def __init__(self):
        self.knowledge = {
            "metadata": {
                "last_updated": datetime.now().isoformat(),
                "version": "1.0",
                "crawler_host": socket.gethostname()
            },
            "infrastructure": {},
            "databases": {},
            "applications": {},
            "network": {},
            "documentation": []
        }

    def crawl_all(self) -> Dict[str, Any]:
        """Execute all crawlers and build complete knowledge base."""
        logger.info("Starting comprehensive infrastructure crawl...")

        self.crawl_local_system()
        self.crawl_network()
        self.crawl_databases()
        self.crawl_applications()
        self.crawl_file_systems()
        self.extract_documentation()

        self.save_knowledge_base()
        logger.info("Crawl complete. Knowledge base saved.")

        return self.knowledge

    def crawl_local_system(self) -> None:
        """Gather local system information."""
        logger.info("Crawling local system...")

        try:
            self.knowledge["infrastructure"]["local_system"] = {
                "hostname": socket.gethostname(),
                "platform": platform.system(),
                "platform_release": platform.release(),
                "platform_version": platform.version(),
                "architecture": platform.machine(),
                "processor": platform.processor(),
                "python_version": platform.python_version(),
                "fqdn": socket.getfqdn(),
                "ip_address": socket.gethostbyname(socket.gethostname())
            }
        except Exception as e:
            logger.error(f"Error crawling local system: {e}")

    def crawl_network(self) -> None:
        """Discover network topology."""
        logger.info("Crawling network topology...")

        network_info = {
            "hostname": socket.gethostname(),
            "fqdn": socket.getfqdn(),
            "interfaces": []
        }

        try:
            # Get network interfaces (platform-specific)
            if platform.system() == "Windows":
                result = subprocess.run(
                    ["ipconfig", "/all"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                network_info["ipconfig_output"] = result.stdout
            else:
                result = subprocess.run(
                    ["ifconfig"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                network_info["ifconfig_output"] = result.stdout

        except Exception as e:
            logger.error(f"Error crawling network: {e}")

        self.knowledge["network"] = network_info


    def crawl_databases(self) -> None:
        """Discover and document database schemas."""
        logger.info("Crawling databases...")

        # Load database credentials
        db_config = {
            "host": os.getenv("DB_HOST", "JEF-SQL"),
            "user": os.getenv("DB_USER", "MAS_REPORTS"),
            "password": os.getenv("DB_PASSWORD", "R3p0rt-M@s-jeffco"),
            "database": os.getenv("DB_NAME", "MAS_JEF")
        }

        if not db_config["user"]:
            logger.warning("No database credentials found. Skipping database crawl.")
            return

        # Try different database types
        self.crawl_sql_server(db_config)
        self.crawl_mysql(db_config)
        self.crawl_postgresql(db_config)

    def _build_sql_server_conn_str(self, config: Dict[str, str]) -> str:
        """Build SQL Server connection string from env/config."""
        conn_str = os.getenv("DB_CONN_STR")
        if conn_str:
            return conn_str

        driver = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
        host = config.get("host", "")
        database = config.get("database", "")
        user = config.get("user", "")
        password = config.get("password", "")

        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={host}",
            f"DATABASE={database}"
        ]

        if user:
            parts.append(f"UID={user}")
            parts.append(f"PWD={password}")
        else:
            parts.append("Trusted_Connection=yes")

        return ";".join(parts) + ";"

    def crawl_sql_server(self, config: Dict[str, str]) -> None:
        """Crawl SQL Server database."""
        try:
            import pyodbc
            logger.info("Attempting SQL Server connection...")

            conn_str = self._build_sql_server_conn_str(config)
            if os.getenv("DB_CONN_STR"):
                logger.info("Using DB_CONN_STR from environment")
            else:
                driver = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
                logger.info(
                    f"Connecting to SQL Server host={config.get('host')} "
                    f"db={config.get('database')} driver={driver}"
                )

            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()

            db_info = {
                "type": "SQL Server",
                "host": config.get("host"),
                "database": config.get("database"),
                "schemas": {},
                "tables": [],
                "views": [],
                "stored_procedures": [],
                "relationships": []
            }

            # Get all tables
            cursor.execute("""
                SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
                FROM INFORMATION_SCHEMA.TABLES
                ORDER BY TABLE_SCHEMA, TABLE_NAME
            """)

            for row in cursor.fetchall():
                schema, table, table_type = row
                if schema not in db_info["schemas"]:
                    db_info["schemas"][schema] = []

                table_info = {
                    "name": table,
                    "schema": schema,
                    "type": table_type,
                    "columns": []
                }

                # Get columns for this table
                cursor.execute(f"""
                    SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                           IS_NULLABLE, COLUMN_DEFAULT
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                    ORDER BY ORDINAL_POSITION
                """, (schema, table))

                for col_row in cursor.fetchall():
                    table_info["columns"].append({
                        "name": col_row[0],
                        "type": col_row[1],
                        "max_length": col_row[2],
                        "nullable": col_row[3],
                        "default": col_row[4]
                    })

                db_info["schemas"][schema].append(table_info)
                db_info["tables"].append(f"{schema}.{table}")

            # Get foreign key relationships
            cursor.execute("""
                SELECT
                    fk.name AS FK_Name,
                    tp.name AS Parent_Table,
                    cp.name AS Parent_Column,
                    tr.name AS Referenced_Table,
                    cr.name AS Referenced_Column
                FROM sys.foreign_keys AS fk
                INNER JOIN sys.foreign_key_columns AS fkc ON fk.object_id = fkc.constraint_object_id
                INNER JOIN sys.tables AS tp ON fkc.parent_object_id = tp.object_id
                INNER JOIN sys.columns AS cp ON fkc.parent_object_id = cp.object_id AND fkc.parent_column_id = cp.column_id
                INNER JOIN sys.tables AS tr ON fkc.referenced_object_id = tr.object_id
                INNER JOIN sys.columns AS cr ON fkc.referenced_object_id = cr.object_id AND fkc.referenced_column_id = cr.column_id
            """)

            for row in cursor.fetchall():
                db_info["relationships"].append({
                    "fk_name": row[0],
                    "parent_table": row[1],
                    "parent_column": row[2],
                    "referenced_table": row[3],
                    "referenced_column": row[4]
                })

            # Get stored procedures
            cursor.execute("""
                SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE
                FROM INFORMATION_SCHEMA.ROUTINES
                WHERE ROUTINE_TYPE = 'PROCEDURE'
            """)

            for row in cursor.fetchall():
                db_info["stored_procedures"].append({
                    "schema": row[0],
                    "name": row[1],
                    "type": row[2]
                })

            conn.close()
            self.knowledge["databases"]["sql_server"] = db_info
            logger.info(f"SQL Server crawl complete: {len(db_info['tables'])} tables found")

        except ImportError:
            logger.warning("pyodbc not installed. Install with: pip install pyodbc")
        except Exception as e:
            logger.error(f"Error crawling SQL Server: {e}")

    def crawl_mysql(self, config: Dict[str, str]) -> None:
        """Crawl MySQL database."""
        try:
            import pymysql
            logger.info("Attempting MySQL connection...")

            conn = pymysql.connect(
                host=config["host"],
                user=config["user"],
                password=config["password"],
                database=config["database"],
                connect_timeout=5
            )
            cursor = conn.cursor()

            db_info = {
                "type": "MySQL",
                "host": config["host"],
                "database": config["database"],
                "tables": [],
                "columns": {}
            }

            # Get all tables
            cursor.execute("SHOW TABLES")
            for row in cursor.fetchall():
                table_name = row[0]
                db_info["tables"].append(table_name)

                # Get columns
                cursor.execute(f"DESCRIBE `{table_name}`")
                db_info["columns"][table_name] = [
                    {
                        "name": col[0],
                        "type": col[1],
                        "null": col[2],
                        "key": col[3],
                        "default": col[4],
                        "extra": col[5]
                    }
                    for col in cursor.fetchall()
                ]

            conn.close()
            self.knowledge["databases"]["mysql"] = db_info
            logger.info(f"MySQL crawl complete: {len(db_info['tables'])} tables found")

        except ImportError:
            logger.warning("pymysql not installed. Install with: pip install pymysql")
        except Exception as e:
            logger.error(f"Error crawling MySQL: {e}")

    def crawl_postgresql(self, config: Dict[str, str]) -> None:
        """Crawl PostgreSQL database."""
        try:
            import psycopg2
            logger.info("Attempting PostgreSQL connection...")

            conn = psycopg2.connect(
                host=config["host"],
                user=config["user"],
                password=config["password"],
                database=config["database"],
                connect_timeout=5
            )
            cursor = conn.cursor()

            db_info = {
                "type": "PostgreSQL",
                "host": config["host"],
                "database": config["database"],
                "schemas": {},
                "tables": []
            }

            # Get all tables
            cursor.execute("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name
            """)

            for schema, table in cursor.fetchall():
                if schema not in db_info["schemas"]:
                    db_info["schemas"][schema] = []

                # Get columns
                cursor.execute("""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """, (schema, table))

                columns = [
                    {
                        "name": row[0],
                        "type": row[1],
                        "nullable": row[2],
                        "default": row[3]
                    }
                    for row in cursor.fetchall()
                ]

                db_info["schemas"][schema].append({
                    "name": table,
                    "columns": columns
                })
                db_info["tables"].append(f"{schema}.{table}")

            conn.close()
            self.knowledge["databases"]["postgresql"] = db_info
            logger.info(f"PostgreSQL crawl complete: {len(db_info['tables'])} tables found")

        except ImportError:
            logger.warning("psycopg2 not installed. Install with: pip install psycopg2")
        except Exception as e:
            logger.error(f"Error crawling PostgreSQL: {e}")

    def crawl_applications(self) -> None:
        """Discover installed applications and services."""
        logger.info("Crawling applications...")

        apps = {
            "python_packages": [],
            "services": [],
            "processes": []
        }

        try:
            # Get Python packages
            result = subprocess.run(
                ["pip", "list", "--format=json"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                apps["python_packages"] = json.loads(result.stdout)

        except Exception as e:
            logger.error(f"Error getting Python packages: {e}")

        try:
            # Get running services (Windows)
            if platform.system() == "Windows":
                result = subprocess.run(
                    ["sc", "query", "state=", "all"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                apps["services_output"] = result.stdout

        except Exception as e:
            logger.error(f"Error getting services: {e}")

        self.knowledge["applications"] = apps

    def crawl_file_systems(self) -> None:
        """Scan relevant file systems for documentation and configs."""
        logger.info("Crawling file systems...")

        # Look for common configuration files
        config_patterns = [
            "*.config",
            "*.json",
            "*.yaml",
            "*.yml",
            "*.ini",
            "*.conf"
        ]

        # Look in current directory and common locations
        search_dirs = [
            os.getcwd(),
            os.path.expanduser("~"),
        ]

        found_configs = []

        for search_dir in search_dirs:
            try:
                for root, dirs, files in os.walk(search_dir):
                    # Skip hidden and system directories
                    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', '__pycache__', 'venv', '.venv']]

                    for file in files:
                        if any(file.endswith(pattern.replace('*', '')) for pattern in config_patterns):
                            filepath = os.path.join(root, file)
                            try:
                                stat = os.stat(filepath)
                                found_configs.append({
                                    "path": filepath,
                                    "size": stat.st_size,
                                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
                                })
                            except Exception:
                                pass

                    # Limit depth
                    if root.count(os.sep) - search_dir.count(os.sep) > 2:
                        break

            except Exception as e:
                logger.error(f"Error scanning {search_dir}: {e}")

        self.knowledge["infrastructure"]["config_files"] = found_configs[:100]  # Limit to 100

    def extract_documentation(self) -> None:
        """Extract documentation from README files, comments, etc."""
        logger.info("Extracting documentation...")

        doc_files = []
        search_dir = os.getcwd()

        try:
            for root, dirs, files in os.walk(search_dir):
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    if file.lower() in ['readme.md', 'readme.txt', 'readme', 'setup.md', 'architecture.md']:
                        filepath = os.path.join(root, file)
                        try:
                            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                                doc_files.append({
                                    "path": filepath,
                                    "filename": file,
                                    "content": content[:10000]  # First 10k chars
                                })
                        except Exception as e:
                            logger.error(f"Error reading {filepath}: {e}")

                if root.count(os.sep) - search_dir.count(os.sep) > 2:
                    break

        except Exception as e:
            logger.error(f"Error extracting documentation: {e}")

        self.knowledge["documentation"] = doc_files

    def save_knowledge_base(self) -> None:
        """Save knowledge base to disk."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save complete knowledge base
        kb_file = os.path.join(KNOWLEDGE_BASE_DIR, f"knowledge_base_{timestamp}.json")
        with open(kb_file, 'w', encoding='utf-8') as f:
            json.dump(self.knowledge, f, indent=2, default=str)

        # Save latest version
        latest_file = os.path.join(KNOWLEDGE_BASE_DIR, "knowledge_base_latest.json")
        with open(latest_file, 'w', encoding='utf-8') as f:
            json.dump(self.knowledge, f, indent=2, default=str)

        logger.info(f"Knowledge base saved to {kb_file}")

        # Generate summary
        self.generate_summary()

    def generate_summary(self) -> None:
        """Generate human-readable summary of discovered infrastructure."""
        summary = []
        summary.append("=" * 60)
        summary.append("INFRASTRUCTURE KNOWLEDGE BASE SUMMARY")
        summary.append("=" * 60)
        summary.append("")

        # System info
        summary.append("LOCAL SYSTEM:")
        sys_info = self.knowledge["infrastructure"].get("local_system", {})
        summary.append(f"  Hostname: {sys_info.get('hostname', 'Unknown')}")
        summary.append(f"  Platform: {sys_info.get('platform', 'Unknown')}")
        summary.append(f"  Architecture: {sys_info.get('architecture', 'Unknown')}")
        summary.append("")

        # Databases
        summary.append("DATABASES:")
        for db_type, db_info in self.knowledge["databases"].items():
            summary.append(f"  {db_type.upper()}:")
            summary.append(f"    Host: {db_info.get('host', 'Unknown')}")
            summary.append(f"    Database: {db_info.get('database', 'Unknown')}")
            summary.append(f"    Tables: {len(db_info.get('tables', []))}")
            if 'stored_procedures' in db_info:
                summary.append(f"    Stored Procedures: {len(db_info['stored_procedures'])}")
            if 'relationships' in db_info:
                summary.append(f"    Foreign Keys: {len(db_info['relationships'])}")
        summary.append("")

        # Applications
        summary.append("APPLICATIONS:")
        apps = self.knowledge.get("applications", {})
        summary.append(f"  Python Packages: {len(apps.get('python_packages', []))}")
        summary.append("")

        # Documentation
        summary.append("DOCUMENTATION:")
        summary.append(f"  Files Found: {len(self.knowledge.get('documentation', []))}")
        summary.append("")

        # Config files
        summary.append("CONFIGURATION FILES:")
        configs = self.knowledge["infrastructure"].get("config_files", [])
        summary.append(f"  Found: {len(configs)}")
        summary.append("")

        summary.append("=" * 60)

        summary_text = "\n".join(summary)
        print(summary_text)

        # Save summary
        summary_file = os.path.join(KNOWLEDGE_BASE_DIR, "summary.txt")
        with open(summary_file, 'w') as f:
            f.write(summary_text)


def main():
    """Run the knowledge crawler."""
    print("Starting Knowledge Base Crawler...")
    print("This will discover and document your infrastructure.")
    print("")

    crawler = KnowledgeCrawler()
    knowledge = crawler.crawl_all()

    print("\nCrawl complete!")
    print(f"Knowledge base saved to: {KNOWLEDGE_BASE_DIR}/")
    print("")
    print("Next steps:")
    print("1. Review the generated knowledge base")
    print("2. Run 'python knowledge_trainer.py' to train the SQL bot")
    print("3. Test with queries to verify accuracy")


if __name__ == "__main__":
    main()
