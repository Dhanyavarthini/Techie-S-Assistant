<picture>
  <img alt="Techie'S Assistant" src="images/Techie.png" height="60">
</picture>

# Techie'S Assistant

## Overview
This project is a **GenAI-based application** designed to assist users in resolving **technical issues with gadgets**. The solution leverages **official and manufacturer websites** to provide accurate and up-to-date information. It uses advanced search capabilities, incorporating **SerpAPI**, **SerperAPI**, and **Chroma** to source and store responses.

### Key Features:
- Provides reliable technical solutions sourced from **official websites**.
- Uses **vector databases** to store search results and streamline future queries.
- Incorporates the power of **Meta LLaMA 3.1 8B** for robust natural language processing.
- Tailored searches by restricting results to **official manufacturer websites**.


### Installation
**Clone the repository**:
   ```bash
   git clone https://github.com/Dhanyavarthini/Techie-S-Assistant.git
   ```
**Setup API keys**:
   Create a `.env` file in the root directory and add the following keys:
   ```
   SERPAPI_KEY=your_serpapi_key
   SAMBANOVA_API_KEY=your_sambanova_key
   ```

**Install and update pip**:
   ```bash
cd ai-starter-kit/search_assistant
python -m venv search_assistant_env
search_assistant_env\Scripts\activate
pip install -r requirements.txt
   ```
**Run the following command:**
   ```bash
streamlit run streamlit/app.py --browser.gatherUsageStats false   

   ```
