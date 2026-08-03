from pathlib import Path

class ProjectReader:

    IGNORE_DIRS = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".vscode",
        "bin",
        "obj",
        "coverage",
        "target",
        "out"
    }

    SOURCE_EXTENSIONS = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
        ".scss",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".cs",
        ".java"
    }

    MAX_FILE_SIZE = 500 * 1024  # 500 KB

    def read_project(self, project_path: str):

        root = Path(project_path)

        project = {
            "folder_structure": [],
            "package_json": "",
            "requirements_txt": "",
            "readme": "",
            "source_files": []
        }


        for path in root.rglob("*"):

            if any(part in self.IGNORE_DIRS for part in path.parts):
                continue

            # Skip directories
            if path.is_dir():
                continue

            relative = str(path.relative_to(root))

            project["folder_structure"].append(relative)

            try:
                if path.stat().st_size > self.MAX_FILE_SIZE:
                    continue
            except Exception:
                continue

            
            if path.name == "package.json":
                project["package_json"] = path.read_text(
                    encoding="utf-8",
                    errors="ignore"
                )

            
            elif path.name == "requirements.txt":
                project["requirements_txt"] = path.read_text(
                    encoding="utf-8",
                    errors="ignore"
                )

            
            elif path.name.lower().startswith("readme"):
                project["readme"] = path.read_text(
                    encoding="utf-8",
                    errors="ignore"
                )

            
            if path.suffix.lower() in self.SOURCE_EXTENSIONS:

                try:

                    content = path.read_text(
                        encoding="utf-8",
                        errors="ignore"
                    )

                    project["source_files"].append({
                        "path": relative,
                        "language": path.suffix.lower(),
                        "content": content
                    })

                except Exception:
                    continue

        return project