# Harness v2 architecture

```
Harness Kernel -> policy + capability/runtime registries + audit/state
               -> OpenCode adapter (optional coding/research provider)
               -> Zen adapter (optional curated-model provider)
               -> Prime adapter (optional durable/IPython/RLM provider)
               -> Hermes adapter (optional messaging/parallel provider)
               -> Local adapter (optional loopback provider)
```

Explicit engine selection outranks policy. Sensitive tasks are loopback-only;
untrusted tasks use the read-only `harness-sandbox` OpenCode agent with plugins
disabled. The audit ledger stores task hashes, never prompt plaintext.
