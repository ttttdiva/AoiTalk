#!/bin/bash
# AoiTalk PostgreSQL Database Setup Script
# This script creates the aoitalk_memory database with pgvector extension

set -e

echo "================================================"
echo "AoiTalk Database Setup"
echo "================================================"
echo ""

# Check if running as root or with sudo
if [ "$EUID" -eq 0 ] || sudo -n true 2>/dev/null; then
    echo "✓ Sudo access confirmed"
else
    echo "This script requires sudo access to create the database."
    echo "You will be prompted for your password."
    echo ""
fi

# Function to execute PostgreSQL commands
run_psql() {
    sudo -u postgres psql -c "$1"
}

# Function to execute PostgreSQL commands on specific database
run_psql_db() {
    sudo -u postgres psql -d "$1" -c "$2"
}

echo "Step 1: Checking if database exists..."
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='aoitalk_memory'" 2>/dev/null || echo "0")

if [ "$DB_EXISTS" = "1" ]; then
    echo "⚠️  Database 'aoitalk_memory' already exists."
    read -p "Do you want to DROP and recreate it? All data will be lost! (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Dropping existing database..."
        run_psql "DROP DATABASE IF EXISTS aoitalk_memory;"
    else
        echo "Keeping existing database."
    fi
fi

echo ""
echo "Step 2: Creating database with UTF8 encoding..."
run_psql "CREATE DATABASE aoitalk_memory WITH OWNER = aoitalk ENCODING = 'UTF8' TEMPLATE = template0;" 2>/dev/null || echo "Database already exists or creation failed"

echo ""
echo "Step 3: Granting privileges..."
run_psql "GRANT ALL PRIVILEGES ON DATABASE aoitalk_memory TO aoitalk;"

echo ""
echo "Step 4: Installing pgvector extension..."
run_psql_db "aoitalk_memory" "CREATE EXTENSION IF NOT EXISTS vector;"

echo ""
echo "Step 5: Verifying setup..."
# Check database
DB_CHECK=$(sudo -u postgres psql -tAc "SELECT datname, pg_encoding_to_char(encoding) FROM pg_database WHERE datname='aoitalk_memory'")
echo "Database: $DB_CHECK"

# Check extension
EXT_CHECK=$(sudo -u postgres psql -d aoitalk_memory -tAc "SELECT extname, extversion FROM pg_extension WHERE extname='vector'")
echo "Extension: $EXT_CHECK"

echo ""
echo "================================================"
echo "✅ Database setup completed!"
echo "================================================"
echo ""
echo "Connection details:"
echo "  Host: localhost"
echo "  Port: 5432"
echo "  Database: aoitalk_memory"
echo "  User: aoitalk"
echo "  Password: (see .env file)"
echo ""
echo "You can now run: python run_local.py"