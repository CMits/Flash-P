# Codex Recipe: run-flashp-studio

Use when the user asks for `/run-flashp-studio` or wants a local browser-style Studio for built networks.

## Script

```bash
python Agent/shared/network_to_studio.py <networks_dir>
```

Default `<networks_dir>` is `networks`.

The script writes:

```text
<networks_dir>/Flash-P_Studio.html
```

It embeds built networks into one self-contained offline HTML file for browsing, viewing DOI-linked graphs, and running perturbation simulations.

