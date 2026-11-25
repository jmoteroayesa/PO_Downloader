# SAP PO Message Explorer - Specification (DRAFT)

> Items marked **[TBD]** need your decision. Edit freely.

## 1. Goal
A small tool that lets a user pick an ICO, search its messages by date/time and
by content, and download the request/response XML files without using the
Message Monitor UI.

## 2. Users
- **[TBD]** Who uses it? (e.g. integration developers, support team, business analysts)
- **[TBD]** Approximate number of users, and are they technical?

## 3. Starting point
Existing script `sap_po_download.py` (Python, standard library only) that:
- Calls `AdapterMessageMonitoringVi` (SOAP): `getIntegrationFlows`, `getMessageList`, `getLoggedMessageBytes`.
- Downloads the first logged version of each message.
- Saves files as `Msg<ReferenceID or MessageID>_<ScenarioName>_yyyymmdd_hhmmss.xml`.

The new tool must reuse this logic and keep this file naming.

## 4. Functional requirements

### Must have
1. **ICO selection:** searchable list of ICO names loaded from `getIntegrationFlows`.
2. **Time range:** from/to date-time pickers.
3. **Message list:** table with message ID, reference ID, start time, status.
4. **Content search:** a text field; only messages whose XML contains the text are kept.
5. **Download:** save selected (or all matching) messages to a user-chosen folder, using the naming above.
6. **Progress and cancel:** visible progress during searches and downloads, with a cancel button.

### Nice to have
- Preview of the XML before saving.
- Case-insensitive and XPath/regex search options.
- Local cache of downloaded payloads, to avoid repeated downloads.
- Export of the message list to CSV.

## 5. Non-functional requirements
- **Security:** no passwords stored in plain text; credentials entered at runtime **[TBD: or Windows Credential Manager]**. TLS verification stays enabled (optional CA file).
- **Authorization:** each user logs in with their own SAP credentials, so SAP access control still applies.
- **Load on PO:** throttle requests; cap the number of messages per search (default 100, configurable).
- **Sensitive data:** files are saved only where the user chooses; no payload content in logs.
- **Platform:** **[TBD]** Windows desktop / internal web app.
- **SAP PO version:** **[TBD]** (needed to confirm `getLoggedMessageBytes` support, SAP Note 2437778).

## 6. Technology (proposal)
Python with a UI layer: Streamlit (web) or Tkinter/PySide (desktop). **[TBD]**
Optional packaging as a single `.exe` with PyInstaller.

## 7. Content search behavior
- SAP's filter works on metadata, not payload, so content search is done locally:
  1. Get messages for the ICO and date range.
  2. Download each message's payload.
  3. Keep those that contain the search text.
- Only retained (logged) payloads can be searched.
- **[TBD]** Is SAP's built-in payload search enabled on your system? If so, it could replace the local approach.

## 8. Limits and edge cases
- The search stops when it reaches the limit (no pagination); the UI should split large date ranges.
- Messages without a logged payload are listed but cannot be searched or downloaded.
- Filename collisions get `_2`, `_3` suffixes; files are never overwritten.
- Errors on one message must not stop the rest; a summary shows how many failed.

## 9. Acceptance criteria (examples)
- Selecting ICO "X" with a one-day range lists all its messages in that window.
- Searching "ORD-123" shows only messages whose XML contains that text.
- Downloading 10 messages creates 10 correctly named XML files in the chosen folder.
- Wrong credentials give a clear error and no partial files.
- Cancelling a search stops further requests to SAP PO promptly.

## 10. Out of scope (for now)
- Resending or editing messages, archived messages, staged (pre-logging) payloads.
- Non-XML payloads (JSON, binary).

## 11. Open questions
- Typical message volume per ICO per day?
- Is the tool allowed to run on a shared server, or only on user PCs?
- Should the file naming be configurable?
