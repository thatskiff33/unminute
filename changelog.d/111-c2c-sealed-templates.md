Security hardening (#111, phase C):

- **Handoff templates are encrypted.** Your edited and custom templates are kept in `templates.sealed`, sealed with AES-256-GCM under the workspace's key, which Windows protects for your account on this computer. A `templates.json` from an earlier version is converted the first time you use templates, keeping every title, prompt and version, and is then deleted. A templates file that cannot be read leaves the built-in templates until you save one.
