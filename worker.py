#!/usr/bin/env python3
"""
Campaign Worker Entry Point

Run this as a separate process to process campaign calls.

Usage:
    python worker.py

The worker will:
1. Check for campaigns with status=RUNNING every 10 seconds
2. Process pending numbers for each campaign
3. Make calls using the user's Twilio credentials
4. Update campaign progress and deduct credits
5. Handle graceful shutdown on SIGTERM/SIGINT
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.campaign_worker import run_worker


if __name__ == "__main__":
    run_worker(check_interval=10)
