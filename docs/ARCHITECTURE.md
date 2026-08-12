# Harness v2 architecture

```
OpenCode (brain/router) -> Harness v2 deterministic control plane
                         -> OpenCode adapter (normal work)
                         -> Zen adapter (curated paid models)
                         -> Prime adapter (durable/IPython/RLM)
                         -> Hermes adapter (messaging/parallel worker)
                         -> Local adapter (loopback, disabled by default)
```

Explicit engine selection outranks policy. Sensitive tasks are loopback-only;
untrusted tasks use the read-only `harness-sandbox` OpenCode agent with plugins
disabled. The audit ledger stores task hashes, never prompt plaintext.
