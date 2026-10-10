# Azure Storage PDF Processing POC

A Python 3.11 proof of concept that uploads PDFs, processes them asynchronously, stores extracted text, and displays processing status in a buildless browser UI.

## Architecture

```text
Browser
   |
   v
FastAPI backend
   |-- uploads/{file_id}/{filename} --------> Blob Storage
   |-- PartitionKey=pdf, RowKey=file_id ----> Table Storage
   `-- serves HTML/CSS/JavaScript UI

BlobCreated event
   |
   v
Event Grid -> pdf-processing queue -> Azure Function
                                      |-- status: Processing
                                      |-- download PDF and extract text
                                      |-- extracted/{file_id}.txt
                                      `-- status: Completed or Failed
```

The backend and Function use one storage account connection string for Blob, Table, and Queue Storage. The Function queue trigger reads `AzureWebJobsStorage`, and its SDK clients read that exact same setting. SDK clients are created once per application or worker process and reused.

## Repository layout

```text
backend/
  app/main.py                 FastAPI application and routes
  app/config.py               Validated environment settings
  app/services/blob_service.py
  app/services/table_service.py
  app/static/                 Plain HTML, CSS, and JavaScript UI
  requirements.txt
function_app/
  function_app.py             Python v2 queue-triggered Function
  host.json                   Queue extension configuration
  sample_event.json           Example BlobCreated event
  requirements.txt
```

## Storage resources

The storage account must contain:

- Blob container `uploads`
- Blob container `extracted`
- Queue `pdf-processing`
- Table `filestatus`

The backend creates missing containers and the table at startup. Create the queue before starting the Function. Event Grid must send `Microsoft.Storage.BlobCreated` events for the uploads container to that queue.

## Environment variables

### Backend

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `AZURE_STORAGE_CONNECTION_STRING` | Yes | — | Connection string used by Blob and Table clients |
| `UPLOAD_CONTAINER` | No | `uploads` | Container for original PDFs |
| `EXTRACTED_CONTAINER` | No | `extracted` | Container for extracted text |
| `TABLE_NAME` | No | `filestatus` | Table containing processing rows |

Copy `backend/.env.example` to `backend/.env`. The backend stops during startup with a clear error when the connection string is missing, empty, or invalid.

### Function app

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `AzureWebJobsStorage` | Yes | — | Shared connection string for the queue trigger, Blob client, and Table client |
| `FUNCTIONS_WORKER_RUNTIME` | Yes | `python` in the example | Selects the Python Functions worker |
| `UPLOAD_CONTAINER` | No | `uploads` | Container accepted by event filtering |
| `EXTRACTED_CONTAINER` | No | `extracted` | Container receiving extracted text |
| `TABLE_NAME` | No | `filestatus` | Table containing processing rows |

Copy `function_app/local.settings.json.example` to `function_app/local.settings.json`. The Function module initializes its clients eagerly and fails worker startup with a clear error if `AzureWebJobsStorage` is unavailable or invalid.

## Protect the connection string

- Treat the connection string as a secret because it contains the storage account key.
- Never commit `backend/.env` or `function_app/local.settings.json`; both are covered by `.gitignore`.
- Do not put the value in source code, logs, exception responses, screenshots, or issue reports.
- Rotate the storage account key and update both applications if the value is exposed.
- In Azure, store it only in application settings. The platform encrypts application settings at rest.

## Run locally

Install Python 3.11 and Azure Functions Core Tools 4.x.

### Start the backend

```powershell
Set-Location backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Replace the placeholder in .env with the storage connection string.
uvicorn app.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000`. Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

### Start the Function

Use a second terminal:

```powershell
Set-Location function_app
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item local.settings.json.example local.settings.json
# Replace the placeholder in local.settings.json with the same connection string.
func start
```

`host.json` keeps `extensions.queues.messageEncoding` set to `none` because Event Grid places plain JSON in the queue.

The included `sample_event.json` demonstrates the expected event shape. Its PDF blob and matching Table row must exist for an end-to-end manual test.

## API

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/api/upload` | Validate and upload one PDF up to 10 MB |
| `GET` | `/api/files` | List PDF rows newest first |
| `GET` | `/api/files/{file_id}` | Return status and completed extracted text |
| `GET` | `/api/files/{file_id}/download` | Stream the original PDF |
| `GET` | `/health` | Report process health |

The Table schema remains:

- `PartitionKey="pdf"`
- `RowKey=file_id`
- `filename`
- `file_type`
- `uploaded_at`
- `status`
- `blob_name`
- `text_blob_name` after successful processing
- `error` after a processing failure

## Deploy to Azure

These commands assume an existing Linux App Service running Python 3.11 and an existing Python 3.11 Function App. Configure secrets in the Azure portal so the value does not enter shell history or command output.

### Configure application settings

For the backend App Service, add:

```text
AZURE_STORAGE_CONNECTION_STRING = <storage connection string>
UPLOAD_CONTAINER                = uploads
EXTRACTED_CONTAINER             = extracted
TABLE_NAME                      = filestatus
SCM_DO_BUILD_DURING_DEPLOYMENT  = true
```

For the Function App, add:

```text
AzureWebJobsStorage     = <the same storage connection string>
FUNCTIONS_WORKER_RUNTIME = python
UPLOAD_CONTAINER         = uploads
EXTRACTED_CONTAINER      = extracted
TABLE_NAME               = filestatus
```

Do not retain any obsolete split settings for `AzureWebJobsStorage`; the queue trigger requires the single exact-name setting shown above.

### Deploy the backend

Set names, configure the Python 3.11 FastAPI startup command, package the contents of `backend`, and deploy them:

```powershell
$ResourceGroup = "<resource-group>"
$WebApp = "<web-app-name>"

az webapp config set --resource-group $ResourceGroup --name $WebApp --linux-fx-version "PYTHON|3.11" --startup-file "gunicorn -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 app.main:app" --output none
Compress-Archive -Path .\backend\* -DestinationPath .\backend.zip -Force
az webapp deploy --resource-group $ResourceGroup --name $WebApp --src-path .\backend.zip --type zip --output none
```

### Deploy the Function

Publish from the Function project directory so Core Tools finds `host.json` and `requirements.txt`:

```powershell
$FunctionApp = "<function-app-name>"

Push-Location function_app
func azure functionapp publish $FunctionApp
Pop-Location
```

After deployment, verify the backend `/health` endpoint, upload a small PDF, and confirm the row progresses through `Uploaded`, `Processing`, and `Completed`.

## Error handling and idempotency

- The upload endpoint validates extension, content type, PDF signature, and the 10 MB size limit.
- If Table insertion fails after Blob upload, the backend attempts to remove the orphaned blob.
- Azure SDK failures become HTTP 503 responses without credential details.
- Unknown file IDs return HTTP 404.
- The Function uploads extracted text with `overwrite=True` and ignores rows already marked `Completed`.
- ETag-guarded transitions prevent concurrent deliveries from downgrading completed work.
- Function exceptions update the row to `Failed` when possible and are re-raised for queue retry and poison-queue behavior.

## Code standards

Apply these rules to all Python contributions:

- Add a module docstring to every Python file.
- Add Google-style docstrings to every class, function, and method.
- Begin each docstring with a one-line imperative summary such as “Upload a PDF to Blob Storage.”
- Document argument meaning and constraints under `Args`, behavior under `Returns`, and expected failures under `Raises`. Add `Example` only when it improves understanding.
- Add full type hints to every callable signature; do not repeat type information in prose.
- Give every Pydantic model a class docstring and every field a meaningful `Field(description=...)` value.
- Document route behavior, success responses, status codes, and error cases in both handler docstrings and FastAPI decorator metadata.
- Use named constants or enums for lifecycle states, partition keys, limits, and other shared values.
- Write inline comments only when they explain why a non-obvious decision exists.
- Never log, return, or commit credentials.

Before submitting changes, run:

```powershell
python -m compileall backend function_app
```
