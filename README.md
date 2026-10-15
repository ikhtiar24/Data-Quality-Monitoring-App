# ClearCheck — data quality monitoring

ClearCheck turns a CSV or Excel workbook into a data-quality dashboard. It profiles
rows, columns, missing values, duplicate rows, inferred types, and column
distributions; evaluates business-user-defined expectations; records each run; and
exports exception reports as Excel and PDF.

## Run locally

Use Python 3.11 (also configured for Streamlit Community Cloud):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Or use the included example dataset: upload [`sample_data.csv`](sample_data.csv),
then add expectations such as:

- `record_id` → Unique values
- `loyalty_score` → Between two numbers → 0 to 100
- `customer_email` → Never null
- `region` → Only allowed values → `North, South, East, West`
- `customer_email` → Matches a regular expression → `^[^@\s]+@[^@\s]+\.[^@\s]+$`

Run the analysis from **Profile data** to save a snapshot and view its exception
report. **Run history** charts the rule pass rate over time and lists saved runs.
To keep the local history database in a different location, set `DQM_DB_PATH`.

## Test

```powershell
python -m unittest discover -s tests -v
```

## Deploy to Streamlit Community Cloud

1. Push this project to a GitHub repository.
2. In [Streamlit Community Cloud](https://share.streamlit.io/), choose **Create app**,
   select the repository, and set the entry point to `app.py`.
3. Choose Python 3.11 if prompted. The `.python-version` file and `requirements.txt`
   provide the runtime and dependencies.
4. Configure the Streamlit app secret `DATABASE_URL` with a PostgreSQL connection
   string (for example, from a managed PostgreSQL provider):

   ```toml
   DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require"
   ```

   Keep the real connection string in Streamlit's app Secrets settings; never commit
   it. ClearCheck uses PostgreSQL when `DATABASE_URL` is set, and local SQLite
   otherwise. A managed database is important for deployment: the local SQLite file
   is convenient for development, but Streamlit Community Cloud's app filesystem is
   not durable across restarts/redeploys.
5. Deploy and verify upload, rule evaluation, history, and report downloads from the
   public app URL.

The repository and cloud account are not connected in this workspace, so publishing
the app and producing its public URL still requires those account-side steps.

## Data handling

Uploaded file contents are analyzed in memory and are not stored in run history.
History stores the filename, aggregate profile, rules, and exception values from
each run. Use synthetic or appropriately authorized data when trying the demo.

