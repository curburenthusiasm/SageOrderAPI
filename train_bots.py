#!/usr/bin/env python3
"""
One-Shot Bot Training Script

Run this script to:
1. Crawl your entire infrastructure
2. Train all bots with discovered knowledge
3. Generate comprehensive documentation
4. Validate the knowledge base

Usage:
    python train_bots.py
"""

import os
import sys
import time
from datetime import datetime


def print_banner():
    """Print startup banner."""
    print("=" * 70)
    print(" " * 20 + "BOT TRAINING SYSTEM")
    print("=" * 70)
    print()
    print("This script will:")
    print("  1. Crawl your infrastructure (databases, servers, files)")
    print("  2. Train SQL Bot with discovered schemas")
    print("  3. Generate training examples and documentation")
    print("  4. Validate knowledge base quality")
    print()
    print("=" * 70)
    print()


def run_step(step_name: str, script: str, description: str) -> bool:
    """Run a training step."""
    print(f"\n{'='*70}")
    print(f"STEP: {step_name}")
    print(f"{'='*70}")
    print(f"{description}")
    print()

    start_time = time.time()

    try:
        print(f"Running: python {script}")
        print()

        # Import and run the module
        if script == "knowledge_crawler.py":
            from knowledge_crawler import KnowledgeCrawler
            crawler = KnowledgeCrawler()
            crawler.crawl_all()
        elif script == "knowledge_trainer.py":
            from knowledge_trainer import KnowledgeTrainer
            trainer = KnowledgeTrainer()
            if not trainer.knowledge:
                print("ERROR: No knowledge base found!")
                print("Crawler may have failed. Check knowledge_crawler.log")
                return False
            trainer.train_all_bots()
        else:
            print(f"Unknown script: {script}")
            return False

        elapsed = time.time() - start_time
        print()
        print(f"✅ {step_name} completed in {elapsed:.1f} seconds")
        return True

    except KeyboardInterrupt:
        print("\n\n⚠️  Training interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error in {step_name}: {e}")
        print("\nCheck the log files for details:")
        print("  - knowledge_crawler.log")
        print("  - ceo_bot.log")
        return False


def validate_knowledge_base() -> bool:
    """Validate the generated knowledge base."""
    print(f"\n{'='*70}")
    print("VALIDATION")
    print(f"{'='*70}")
    print("Checking knowledge base quality...")
    print()

    issues = []

    # Check if knowledge base exists
    kb_file = "knowledge_base/knowledge_base_latest.json"
    if not os.path.exists(kb_file):
        issues.append("❌ Knowledge base not found")
    else:
        import json
        with open(kb_file, 'r') as f:
            kb = json.load(f)

        # Check databases
        if not kb.get("databases"):
            issues.append("⚠️  No databases discovered")
        else:
            db_count = len(kb["databases"])
            print(f"✅ Found {db_count} database(s)")

            for db_type, db_info in kb["databases"].items():
                table_count = len(db_info.get("tables", []))
                print(f"   - {db_type}: {table_count} tables")

        # Check infrastructure
        if kb.get("infrastructure", {}).get("local_system"):
            sys_info = kb["infrastructure"]["local_system"]
            print(f"✅ System info: {sys_info.get('hostname', 'Unknown')}")
        else:
            issues.append("⚠️  No system information discovered")

        # Check documentation
        doc_count = len(kb.get("documentation", []))
        if doc_count > 0:
            print(f"✅ Found {doc_count} documentation file(s)")
        else:
            issues.append("⚠️  No documentation found")

    # Check training data
    print()
    training_files = [
        "training_data/sql_bot_training.json",
        "training_data/sql_quick_reference.txt",
        "training_data/sql_bot_context.json"
    ]

    for file in training_files:
        if os.path.exists(file):
            print(f"✅ {file}")
        else:
            issues.append(f"❌ {file} not found")

    print()

    if issues:
        print("Issues found:")
        for issue in issues:
            print(f"  {issue}")
        print()
        print("Some components may not have been discovered.")
        print("Check that:")
        print("  - Database credentials are in .env file")
        print("  - Database is accessible from this machine")
        print("  - Required pip packages are installed")
        return False
    else:
        print("✅ All validation checks passed!")
        return True


def print_summary():
    """Print training summary."""
    print(f"\n{'='*70}")
    print("TRAINING COMPLETE!")
    print(f"{'='*70}")
    print()
    print("Files created:")
    print()

    print("Knowledge Base:")
    if os.path.exists("knowledge_base/knowledge_base_latest.json"):
        print("  ✅ knowledge_base/knowledge_base_latest.json")
        print("  ✅ knowledge_base/summary.txt")
    else:
        print("  ❌ Knowledge base not found")

    print()
    print("Training Data:")
    training_files = [
        "training_data/sql_bot_training.json",
        "training_data/sql_quick_reference.txt",
        "training_data/sql_bot_context.json",
        "training_data/common_queries.json",
        "training_data/training_examples.json"
    ]

    for file in training_files:
        status = "✅" if os.path.exists(file) else "❌"
        print(f"  {status} {file}")

    print()
    print("Next steps:")
    print()
    print("1. Review the knowledge base:")
    print("   cat knowledge_base/summary.txt")
    print()
    print("2. Check SQL bot training:")
    print("   cat training_data/sql_quick_reference.txt")
    print()
    print("3. Start the CEO Bot:")
    print("   python CEO.py")
    print()
    print("4. Test with a question:")
    print("   CEO Bot > answer: how many records in customers table?")
    print()
    print("=" * 70)


def check_prerequisites() -> bool:
    """Check if prerequisites are met."""
    print("Checking prerequisites...")
    print()

    all_good = True

    # Check Python version
    if sys.version_info < (3, 8):
        print("❌ Python 3.8+ required")
        all_good = False
    else:
        print(f"✅ Python {sys.version_info.major}.{sys.version_info.minor}")

    # Check required modules
    # pywin32 installs as win32com, win32api, etc., not as "pywin32"
    required = {
        "ollama": "ollama",
        "win32com.client": "pywin32"  # pywin32 imports as win32com
    }
    optional = {
        "pyodbc": "pyodbc",
        "pymysql": "pymysql",
        "psycopg2": "psycopg2"
    }

    for import_name, package_name in required.items():
        try:
            __import__(import_name)
            print(f"✅ {package_name}")
        except ImportError:
            print(f"❌ {package_name} not installed (required)")
            print(f"   Install with: pip install {package_name}")
            all_good = False

    for import_name, package_name in optional.items():
        try:
            __import__(import_name)
            print(f"✅ {package_name}")
        except ImportError:
            print(f"⚠️  {package_name} not installed (optional, for database crawling)")

    # Check .env file
    print()
    if os.path.exists(".env"):
        print("✅ .env file found")

        # Check if DB credentials are set
        from dotenv import load_dotenv
        load_dotenv()

        if os.getenv("DB_USER"):
            print("✅ Database credentials configured")
        else:
            print("⚠️  Database credentials not configured")
            print("   The crawler will skip database discovery")
    else:
        print("⚠️  .env file not found")
        print("   Copy .env to .env and configure credentials")

    print()

    if not all_good:
        print("❌ Prerequisites not met. Please install required packages.")
        return False

    print("✅ Prerequisites OK")
    print()
    return True


def main():
    """Main training orchestrator."""
    print_banner()

    # Check prerequisites
    if not check_prerequisites():
        print("\nPlease install required packages and try again.")
        return

    input("Press ENTER to start training...")
    print()

    start_time = datetime.now()

    # Step 1: Crawl infrastructure
    if not run_step(
        "1. Infrastructure Crawl",
        "knowledge_crawler.py",
        "Discovering databases, servers, applications, and documentation..."
    ):
        print("\n❌ Training failed at infrastructure crawl")
        return

    # Step 2: Train bots
    if not run_step(
        "2. Bot Training",
        "knowledge_trainer.py",
        "Generating training examples, context files, and documentation..."
    ):
        print("\n❌ Training failed at bot training")
        return

    # Step 3: Validate
    validation_passed = validate_knowledge_base()

    # Print summary
    print_summary()

    elapsed = datetime.now() - start_time
    print(f"\nTotal time: {elapsed.total_seconds():.1f} seconds")
    print()

    if validation_passed:
        print("🎉 Bot training successful!")
        print()
        print("Your bots are now trained and ready to answer questions!")
    else:
        print("⚠️  Training completed with some issues")
        print()
        print("Review the issues above and re-run if needed.")


if __name__ == "__main__":
    try:
        # Check if dotenv is available
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            print("Installing python-dotenv...")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "python-dotenv"])
            from dotenv import load_dotenv
            load_dotenv()

        main()

    except KeyboardInterrupt:
        print("\n\nTraining cancelled by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {e}")
        print("\nCheck log files for details")
        sys.exit(1)
