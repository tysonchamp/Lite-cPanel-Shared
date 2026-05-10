# Lite-cPanel: AI Assistant Project Guidelines

This document establishes strict, non-negotiable guidelines for AI assistants contributing to the Lite-cPanel project. These rules ensure security, maintainability, and a premium user experience.

---

## 🛡️ Security First (Non-Negotiable)

1.  **Input Validation**: ALL user-provided input must be validated and sanitized before use in shell commands, database queries, or file system operations.
2.  **Path Boundaries**: Prevent directory traversal. Always use `os.path.abspath` and verify that the resulting path is within the intended directory (e.g., `/var/www/` or `/var/lib/lite-cpanel/`).
3.  **Command Execution**: 
    - Favor Python-native libraries (e.g., `os`, `shutil`, `psutil`) over `subprocess.run(shell=True)`.
    - If shell execution is required, use arrays for arguments to avoid injection.
4.  **Credential Management**:
    - **NEVER** hardcode passwords or API keys.
    - Sensitive data must be stored in `/var/lib/lite-cpanel/` with `600` permissions.
    - Use `openssl rand -base64` or equivalent for generating secure credentials.
5.  **Root Access**: Scripts must check for root privileges (`[ "$EUID" -ne 0 ]`) and fail gracefully if not present.

---

## 💻 Scripting Standards (Bash & Python)

### Bash
- **Modularization**: Break logic into clear, single-responsibility functions.
- **Feedback**: Use ANSI colors (`RED`, `GREEN`, `YELLOW`) for all console output.
- **Automation**: Use `debconf-set-selections` for non-interactive package installations. No `read` prompts during automated `install.sh` runs unless explicitly requested.
- **Portability**: Target **Ubuntu 24.04 (Noble)** specifically.

### Python (Flask)
- **Framework**: Use Flask 3.x.
- **Structure**: The main application file (`cpanel.py`) must remain small and act strictly as an initialization/bootstrap script. All routing logic **MUST** be separated into specific Flask Blueprints within the `cpanel/app/lib/` directory.
- **Error Handling**: Use try-except blocks with logging. Never return raw stack traces to the UI.

---

## 🏢 Shared Hosting & Multi-Tenancy

1.  **Role-Based Access Control (RBAC)**: Maintain strict isolation between `admin` (root) and `user` accounts. Non-admin users must not see or access global server controls.
2.  **Resource Constraints**: All shared hosting users must have their resources (Domains, Databases, Next.js Apps) validated against their assigned Hosting Plan limits.
3.  **Filesystem Jail**: User operations (File Manager, FTP, Document Roots) must be strictly confined to their respective `/home/{username}/` directories.
4.  **Database Namespaces**: To prevent collisions, all user databases must be strictly prefixed with their username (e.g., `{username}_{dbname}`).

---

## 🎨 Design & UI/UX (Premium Aesthetics)

1.  **Visual Excellence**: Every UI change must feel "Premium" and "Modern."
    - Use **Glassmorphism** (backdrop-filter: blur) where appropriate.
    - Use **Dark Mode** by default or as a primary option.
    - Use curated color palettes (no plain `red` or `blue`).
2.  **Typography**: Use modern Google Fonts (e.g., *Inter*, *Outfit*, *Roboto*).
3.  **No Placeholders**: Never use placeholder images. Generate actual assets using `generate_image`.
4.  **Micro-animations**: Use subtle CSS transitions for hover states and modal entries.
5.  **Vanilla CSS**: Prefer Vanilla CSS over Tailwind unless the user explicitly requests it.

---

## 📂 Project Architecture

- **`scripts/`**: All system-level orchestration scripts.
- **`cpanel/`**: The web application and its Python requirements.
  - **`cpanel/app/cpanel.py`**: The main bootstrap file.
  - **`cpanel/app/lib/`**: Contains all modular Flask Blueprints (`routes_*.py`).
- **`/var/lib/lite-cpanel/`**: Persistent data, internal configs, passwords, and the `hosting.db` SQLite database.
- **`/etc/apache2/sites-available/`**: Virtual host management target.

---

## 🤖 AI Interaction Rules

- **No Half-Measures**: Do not provide "examples" or "snippets" when asked for features. Implement the complete, production-ready solution.
- **Documentation**: Update `RELEASE-NOTE.md` and relevant docstrings with every major change.
- **Verification**: Always verify changes by checking file existence, permissions, or running syntax checks (e.g., `bash -n` for scripts).

> [!IMPORTANT]
> Failure to adhere to these security and design standards is considered a failure of the task. If a request conflicts with security best practices, the AI must warn the user and propose a secure alternative.
