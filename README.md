AI-Powered Distributed Backup SystemTeam Dhurandhar— MSpectra 2026 
high-performance, asynchronous backup solution featuring real-time file watching, metadata persistence, and AI-driven file prioritization using the Groq LLM. This system is designed with a "Hybrid Cloud" architecture: a local high-speed backup engine synced with a cloud-hosted metadata dashboard.

 Problem StatementIn fast-paced development environments, manual backups are often forgotten, leading to critical data loss. Existing solutions are either too heavy for local machines or too disconnected from the cloud.Our Goal: To build a lightweight, "set-and-forget" backup system that monitors local changes in real-time and provides a centralized cloud dashboard for observability, using AI to help users identify their most important files during a recovery phase
 
 The Team: Dhurandhar
 Sumit Bhushan Singh 
 Chirag N 
 Kavya Bajaj 

 live deployment is accessible at:

https://team-dhurandar-api.onrender.com📡

├── backup/             # Core logic & Scheduler
├── server/             # FastAPI Backend & Client
├── frontend/           # React Dashboard
├── tests/              # Sample directory for backup
└── main.py             # Entry point
