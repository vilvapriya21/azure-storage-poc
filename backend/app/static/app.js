"use strict";

const POLL_INTERVAL_MS = 3000;
const MAX_FILE_SIZE = 10 * 1024 * 1024;
const STATUS_CLASSES = {
  Uploaded: "status-uploaded",
  Processing: "status-processing",
  Completed: "status-completed",
  Failed: "status-failed",
};

const uploadForm = document.querySelector("#upload-form");
const fileInput = document.querySelector("#pdf-file");
const fileName = document.querySelector("#file-name");
const uploadButton = document.querySelector("#upload-button");
const uploadMessage = document.querySelector("#upload-message");
const progressWrap = document.querySelector("#upload-progress-wrap");
const progressTrack = progressWrap.querySelector("[role='progressbar']");
const progressBar = document.querySelector("#upload-progress");
const filesBody = document.querySelector("#files-body");
const filesEmpty = document.querySelector("#files-empty");
const filesError = document.querySelector("#files-error");
const refreshIndicator = document.querySelector("#refresh-indicator");
const detailPlaceholder = document.querySelector("#detail-placeholder");
const detailContent = document.querySelector("#detail-content");
const detailFilename = document.querySelector("#detail-filename");
const detailStatus = document.querySelector("#detail-status");
const detailDate = document.querySelector("#detail-date");
const extractedText = document.querySelector("#extracted-text");
const downloadLink = document.querySelector("#download-link");
const detailError = document.querySelector("#detail-error");

let selectedFileId = null;
let filesRequestInFlight = false;
let detailRequestNumber = 0;

function setMessage(element, message, type = "") {
  element.textContent = message;
  element.classList.toggle("message-error", type === "error");
  element.classList.toggle("message-success", type === "success");
}

function setStatusBadge(element, status) {
  const safeStatus = typeof status === "string" && status ? status : "Unknown";
  element.textContent = safeStatus;
  element.className = "status-badge";
  const statusClass = STATUS_CLASSES[safeStatus];
  if (statusClass) {
    element.classList.add(statusClass);
  }
}

function formatDate(value) {
  if (typeof value !== "string" || !value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

async function responseError(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
  } catch (_error) {
    // Use the status fallback for a non-JSON response.
  }
  return `Request failed (${response.status})`;
}

function renderFiles(files) {
  filesBody.replaceChildren();
  filesEmpty.hidden = files.length !== 0;

  for (const file of files) {
    const fileId = typeof file.RowKey === "string" ? file.RowKey : "";
    const displayName = typeof file.filename === "string" ? file.filename : "Untitled PDF";
    const row = document.createElement("tr");
    row.className = "file-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", `View details for ${displayName}`);
    row.dataset.fileId = fileId;
    row.classList.toggle("file-row-selected", fileId === selectedFileId);

    const filenameCell = document.createElement("td");
    filenameCell.className = "filename-cell";
    filenameCell.textContent = displayName;
    const dateCell = document.createElement("td");
    dateCell.className = "date-cell";
    dateCell.textContent = formatDate(file.uploaded_at);
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    setStatusBadge(badge, file.status);
    statusCell.append(badge);

    row.append(filenameCell, dateCell, statusCell);
    if (fileId) {
      const selectRow = () => selectFile(fileId);
      row.addEventListener("click", selectRow);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectRow();
        }
      });
    }
    filesBody.append(row);
  }
}

async function loadFiles() {
  if (filesRequestInFlight) return;
  filesRequestInFlight = true;
  refreshIndicator.textContent = "Refreshing…";
  try {
    const response = await fetch("/api/files", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(await responseError(response));
    const files = await response.json();
    if (!Array.isArray(files)) throw new Error("The file list response was invalid.");
    renderFiles(files);
    filesError.hidden = true;
    refreshIndicator.textContent = "Up to date";
    if (selectedFileId) await loadFileDetail(selectedFileId, false);
  } catch (error) {
    filesError.textContent = error instanceof Error ? error.message : "Could not load files.";
    filesError.hidden = false;
    refreshIndicator.textContent = "Refresh failed";
  } finally {
    filesRequestInFlight = false;
  }
}

async function selectFile(fileId) {
  selectedFileId = fileId;
  for (const row of filesBody.querySelectorAll(".file-row")) {
    row.classList.toggle("file-row-selected", row.dataset.fileId === fileId);
  }
  await loadFileDetail(fileId, true);
}

async function loadFileDetail(fileId, showLoading) {
  const requestNumber = ++detailRequestNumber;
  detailError.hidden = true;
  if (showLoading) {
    detailPlaceholder.hidden = true;
    detailContent.hidden = false;
    detailFilename.textContent = "Loading…";
    detailDate.textContent = "—";
    setStatusBadge(detailStatus, "Loading");
    extractedText.textContent = "Loading file details…";
    downloadLink.hidden = true;
  }

  try {
    const response = await fetch(`/api/files/${encodeURIComponent(fileId)}`, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(await responseError(response));
    const file = await response.json();
    if (requestNumber !== detailRequestNumber || fileId !== selectedFileId) return;

    detailPlaceholder.hidden = true;
    detailContent.hidden = false;
    detailFilename.textContent = typeof file.filename === "string" ? file.filename : "Untitled PDF";
    detailDate.textContent = formatDate(file.uploaded_at);
    setStatusBadge(detailStatus, file.status);
    if (typeof file.extracted_text === "string") {
      extractedText.textContent = file.extracted_text || "No text was found in this PDF.";
    } else if (file.status === "Failed") {
      extractedText.textContent = typeof file.error === "string" && file.error
        ? `Processing failed: ${file.error}`
        : "Text extraction failed.";
    } else {
      extractedText.textContent = "Extracted text will appear when processing is complete.";
    }
    downloadLink.href = `/api/files/${encodeURIComponent(fileId)}/download`;
    downloadLink.hidden = false;
  } catch (error) {
    if (requestNumber !== detailRequestNumber) return;
    detailError.textContent = error instanceof Error ? error.message : "Could not load file details.";
    detailError.hidden = false;
  }
}

function validateFile(file) {
  if (!file) return "Choose a PDF to upload.";
  if (!file.name.toLowerCase().endsWith(".pdf") || file.type !== "application/pdf") {
    return "Only PDF files are accepted.";
  }
  if (file.size > MAX_FILE_SIZE) return "PDF files must be 10 MB or smaller.";
  return null;
}

function uploadFile(file) {
  const formData = new FormData();
  formData.append("file", file);
  const request = new XMLHttpRequest();
  request.open("POST", "/api/upload");
  request.responseType = "json";
  request.setRequestHeader("Accept", "application/json");

  uploadButton.disabled = true;
  progressWrap.hidden = false;
  progressBar.style.width = "0%";
  progressTrack.setAttribute("aria-valuenow", "0");
  setMessage(uploadMessage, "Uploading…");

  request.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.round((event.loaded / event.total) * 100);
    progressBar.style.width = `${percent}%`;
    progressTrack.setAttribute("aria-valuenow", String(percent));
    setMessage(uploadMessage, `Uploading… ${percent}%`);
  });

  request.addEventListener("load", async () => {
    uploadButton.disabled = false;
    if (request.status >= 200 && request.status < 300) {
      progressBar.style.width = "100%";
      progressTrack.setAttribute("aria-valuenow", "100");
      setMessage(uploadMessage, "Upload complete. Processing will begin shortly.", "success");
      uploadForm.reset();
      fileName.textContent = "No file selected";
      await loadFiles();
      return;
    }
    const detail = request.response && typeof request.response.detail === "string"
      ? request.response.detail
      : `Upload failed (${request.status}).`;
    setMessage(uploadMessage, detail, "error");
  });
  request.addEventListener("error", () => {
    uploadButton.disabled = false;
    setMessage(uploadMessage, "Upload failed. Check your connection and try again.", "error");
  });
  request.addEventListener("abort", () => {
    uploadButton.disabled = false;
    setMessage(uploadMessage, "Upload was cancelled.", "error");
  });
  request.send(formData);
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileName.textContent = file ? file.name : "No file selected";
  setMessage(uploadMessage, "");
});

uploadForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  const error = validateFile(file);
  if (error) {
    setMessage(uploadMessage, error, "error");
    return;
  }
  uploadFile(file);
});

loadFiles();
window.setInterval(loadFiles, POLL_INTERVAL_MS);
