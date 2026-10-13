# HROne Employee Attendance & Analytics API
A FastAPI application for employee attendance tracking and analytics using MongoDB.

## Features
- Employee listing and attendance management
- Punch-in and punch-out endpoints
- Monthly employee attendance analytics
- Department attendance summaries
- Late-arrival leaderboard
- Department attendance trends

## Requirements
- Python 3.8 or compatible
- MongoDB 6.0 or newer
- Dependencies in requirements.txt

## Local Setup
1. Create a virtual environment:
   python -m venv venv
   .\venv\Scripts\Activate.ps1

2. Install dependencies:
   pip install -r requirements.txt

3. Configure MONGO_URI and MONGO_DB through environment variables or a local .env file. Never commit secrets.

4. Start the API:
   uvicorn app.main:app --port 8000

## API Documentation
- Health check: http://127.0.0.1:8000/health
- Swagger UI: http://127.0.0.1:8000/docs

## Project Documentation
See openapi.yaml and DATA_MODEL.md for the API contract and data model.
See REVIEW.md and DECISIONS.md for implementation review and design decisions.

## Security
Do not commit .env files, credentials, virtual environments, or other sensitive information.
