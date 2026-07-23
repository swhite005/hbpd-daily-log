# HBPD Daily Activity Log Generator

A web app for generating formatted daily activity log Word documents from pasted incident data.

---

## Project Structure

```
hbpd-daily-log/
├── frontend/
│   └── index.html          ← GitHub Pages front end (the web app)
├── backend/
│   ├── app.py              ← Flask API
│   ├── requirements.txt
│   ├── render.yaml         ← Render deployment config
│   └── DAILY_LOG_TEMPLATE.dotx  ← ⚠️ YOU MUST ADD THIS FILE
└── README.md
```

---

## One-Time Setup

### Step 1 — Prepare the repository

1. Go to [github.com](https://github.com) and create a new repository named `hbpd-daily-log`
2. Upload all files from this project into the repository
3. **Important:** Copy your `DAILY_LOG_TEMPLATE.dotx` file into the `backend/` folder and upload it too

### Step 2 — Add supervisors

Open `backend/app.py` and find this section near the top:

```python
SUPERVISORS = [
    "Lieutenant Shawn White",
    "Lieutenant John Smith",       # Replace with real names
    "Lieutenant Jane Doe",         # Replace with real names
    "Sergeant Mike Johnson",       # Replace with real names
]
```

Replace the placeholder names with your actual supervisors. Save and re-upload.

### Step 3 — Deploy the backend to Render

1. Go to [render.com](https://render.com) and sign up with your GitHub account
2. Click **New → Web Service**
3. Connect your `hbpd-daily-log` GitHub repository
4. Set the **Root Directory** to `backend`
5. Render will auto-detect the settings from `render.yaml`
6. Click **Create Web Service**
7. Wait for the deploy to finish (2–3 minutes)
8. Copy your service URL — it will look like: `https://hbpd-daily-log-api.onrender.com`

### Step 4 — Connect the front end to the backend

1. Open `frontend/index.html`
2. Find this line near the bottom:
   ```javascript
   const BACKEND_URL = "https://YOUR-RENDER-SERVICE.onrender.com";
   ```
3. Replace `YOUR-RENDER-SERVICE` with your actual Render URL from Step 3
4. Save the file and upload it to GitHub

### Step 5 — Enable GitHub Pages

1. In your GitHub repository, go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Set branch to `main` and folder to `/frontend`
4. Click **Save**
5. Your app will be live at: `https://YOUR-GITHUB-USERNAME.github.io/hbpd-daily-log/`

---

## Usage

1. Open the web app URL
2. Select the supervisor from the dropdown
3. Paste incident report data into the text box (or drag and drop a .txt file)
4. Click **Generate Daily Log**
5. The `.docx` file downloads automatically, named with today's date (e.g. `07-22-2026.docx`)

---

## Adding or Removing Supervisors

Edit the `SUPERVISORS` list in `backend/app.py`, commit the change to GitHub, and Render will automatically redeploy.

---

## Notes

- The template supports up to 13 incidents per log
- Residential addresses are automatically converted to thousand-block format
- Image references in pasted data are automatically stripped
- The date field is always set to today's generation date
- Officers and Arrested fields are not included in the output (by design)
