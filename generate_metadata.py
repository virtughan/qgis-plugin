#!/usr/bin/env python3

import tomllib
from pathlib import Path

def generate_metadata():
    """Generate QGIS metadata.txt from pyproject.toml"""
    
    with open("pyproject.toml", "rb") as f:
        config = tomllib.load(f)
    
    project = config["project"]
    qgis_config = config.get("tool", {}).get("qgis-plugin", {})
    
    metadata_content = f"""[general]
name={qgis_config.get("name", project["name"])}
description={qgis_config.get("description", project["description"])}
about={qgis_config.get("about", project["description"])}
version={project["version"]}
qgisMinimumVersion={qgis_config.get("qgis_minimum_version", "3.22")}
qgisMaximumVersion={qgis_config.get("qgis_maximum_version", "4.99")}
author={"; ".join([author.get("name", "") for author in project.get("authors", [])])}
email={project.get("authors", [{}])[0].get("email", "")}
category={qgis_config.get("category", "Analysis")}
icon={qgis_config.get("icon", "")}
homepage={project.get("urls", {}).get("Homepage", "")}
tracker={project.get("urls", {}).get("Issues", "")}
repository={project.get("urls", {}).get("Repository", "")}
experimental={str(qgis_config.get("experimental", False))}
deprecated={str(qgis_config.get("deprecated", False))}
"""
    
    output_path = Path("virtughan_qgis/metadata.txt")
    output_path.write_text(metadata_content)
    print(f"Generated {output_path}")

if __name__ == "__main__":
    generate_metadata()
