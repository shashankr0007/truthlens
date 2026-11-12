# TruthLens – domain chatbot with hallucination scoring
pip install -r requirements.txt
copy .env.example to .env and add your key
uvicorn main:app --reload   ->  http://127.0.0.1:8000  (API docs at /docs)
Edit knowledge.md to change the bot's knowledge (no code changes).
