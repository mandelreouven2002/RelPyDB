# RelPyDB documentation site

This site is organized as developer documentation for RelPyDB. It includes:

- A full capability checklist covering the complete feature map.
- Python Integration guide.
- Why RelPyDB positioning page.
- Expanded exports documentation with whole-table, single-column and primary-key-row examples.
- Richer Querying, Grouping, Data Operations, Views, Indexes, Persistence, Encryption and API Reference pages.
- Labs embedded inside the docs flow.

Run locally:

```bash
python -m http.server 8000
```

Then open `http://localhost:8000`.


New in this version:
- docs/api-functions.html - full function reference with inputs, outputs, examples and import guidance.

Server edition (new):
- docs/databases.html - real SQL backends (SQLite, PostgreSQL, MySQL, Oracle, Azure, AWS), connection URLs, reflecting an existing database, raw SQL, and limitations.
- docs/server.html - the HTTP server (programmatic, CLI, WSGI), the remote client, token auth, and error handling.
- Home, installation, capabilities and errors pages updated to cover backends, the server/remote client, driver extras, and the new BackendError/RemoteError family.
