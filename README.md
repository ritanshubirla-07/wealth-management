# VIKABH - Wealth Intelligence

VIKABH is an AI-powered portfolio analytics and intelligence dashboard. It extracts financial data from raw Demat and PMS statement PDFs, saves it to a local SQLite database, crunches the analytics, and queries Generative AI (LLMs) to automatically write comprehensive financial narratives and risk alerts. 

The entire system is wrapped in a lean, lightning-fast Vanilla JS frontend dashboard that flawlessly matches enterprise-grade BI tools.

---

## 🚀 Features

* **Client CRUD:** Manage multiple clients/families from a central directory.
* **Intelligent PDF Upload:** Drag-and-drop NSDL/CDSL Demat and PMS statements. PyMuPDF extracts the exact asset allocation, buy prices, and quantities.
* **LLM Engine:** Streams the parsed data through OpenAI / Scaleway endpoints to generate bespoke AI narratives for Performance, Risk, and Overall Health.
* **Dynamic Analytics Engine:** Calculates HHI Index (Concentration Risk), Asset overlaps, weighted average returns, and sector exposures on the fly.
* **Lean SPA Dashboard:** A zero-dependency Vanilla JS frontend that instantly pivots data when switching between the Family level and individual sub-accounts.

## 🧠 How the AI Backend Works

VIKABH uses a specialized, multi-stage pipeline to convert unstructured PDFs into intelligent financial narratives without hallucinating:

1. **Deterministic Parsing (PyMuPDF):** When a user uploads a Demat or PMS statement, our custom parsers (`app/parsers/`) extract the raw text. Because financial PDFs are notoriously complex, the engine relies on strict Regex and vertical block tracking to deterministically extract Exact Quantities, Buy Prices, and Current Market Values.
2. **Aggregated Math (SQLAlchemy):** The extracted holdings are saved to SQLite. The Python engine calculates the true performance metrics (Weighted Average Returns, HHI Concentration Index, Top Winners/Losers) using raw math, **not AI**. This guarantees that the numbers you see on the dashboard are 100% mathematically accurate.
3. **Targeted LLM Orchestration:** Instead of dumping the entire portfolio into one massive AI prompt (which degrades quality), `llm.py` orchestrates **4 isolated LLM calls** per account:
   * **Overview AI:** Analyzes high-level asset allocation.
   * **Performance AI:** Analyzes the biggest winners/losers and generates a performance summary.
   * **Risk AI:** Looks exclusively at HHI concentration and cross-account overlaps to warn about vulnerabilities.
   * **Insights AI:** Generates 3-4 distinct actionable alerts (Danger/Warning/Success) based on the overall portfolio health.
4. **Zero-Latency Dashboard (Caching):** LLMs take 15-20 seconds to process. To ensure the frontend dashboard feels as fast as PowerBI, the FastAPI backend processes the LLM calls in a background thread and saves the outputs directly into an `AnalysisCache` table. The frontend simply queries this cache, resulting in sub-millisecond load times when switching tabs.

---

## 🛠️ Tech Stack

* **Backend:** Python, FastAPI, SQLAlchemy, SQLite, PyMuPDF
* **AI:** `requests` (provider-agnostic), explicitly optimized for `gpt-4o-mini`, `gpt-oss-120b`, or `llama-3.3-70b-instruct`. 
* **Frontend:** Vanilla HTML/JS/CSS, Chart.js

---

## ⚙️ Setup & Installation

### 1. Clone the Repository
```bash
git clone <your-repo-url>
cd wealthview-lite
```

### 2. Set Up the Virtual Environment
```bash
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Mac/Linux

pip install -r requirements.txt
```

### 3. Configure the Environment (.env)
Create a `.env` file in the root directory (you can use `.env.example` as a template):
```ini
# Defaults to OpenAI
OPENAI_API_KEY=sk-your-openai-key

# OR override with Scaleway / Custom LLMs
LLM_BASE_URL=https://api.scaleway.ai/v1/chat/completions
LLM_API_KEYS=cf8ab50d-f68e-4be7-a558-f0ae074dc627
LLM_MODEL=llama-3.3-70b-instruct
```

### 4. Run the Server
Boot up the FastAPI server. The SQLite database (`VIKABH.db`) will automatically initialize on startup.
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 🖥️ Usage Flow

1. **Open the App:** Navigate to `http://localhost:8000/` in your browser. You will land on the Client Directory.
2. **Create a Client:** Click "New Client", enter the Family Name, and hit save. 
3. **Upload Statements:** Click the client card to open the Staging Area. Drag and drop the specific Demat or PMS PDFs for that client.
4. **Trigger AI Analysis:** Click "Analyze Portfolio". The AI engine will spin for ~15 seconds while parsing the PDFs and hitting the LLM API to write the narratives.
5. **View Dashboard:** Once the AI finishes, you will be redirected to the Dynamic Dashboard.
6. **Pivot Accounts:** Use the dropdown in the top-right corner to toggle smoothly between the "Family Portfolio" (aggregated) and the individual underlying Demat/PMS accounts!
