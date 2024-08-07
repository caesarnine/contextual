import argparse
import logging
import os
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Set

import pathspec
from git import GitCommandError

from .chat_mode import ChatMode
from .preset_manager import (
    get_presets,
    load_presets,
    save_built_in_presets,
    view_presets,
)
from .version_control import initialize_version_control

# Default exclude patterns
DEFAULT_EXCLUDE = [".*"]  # This will exclude all dotfiles and folders

# Set up logging
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool):
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(levelname)s - %(message)s"
    )


def detect_project_types(folder_path: str) -> Set[str]:
    detected_types = set()

    for root, _, files in os.walk(folder_path):
        for file in files:
            file_lower = file.lower()

            # Check prefixes
            for project_type, preset in get_presets().items():
                if "prefixes" in preset and any(
                    file_lower.startswith(prefix.lower())
                    for prefix in preset["prefixes"]
                ):
                    detected_types.add(project_type)
                    continue

            # Check suffixes
            _, ext = os.path.splitext(file)
            if ext:
                for project_type, preset in get_presets().items():
                    if ext in preset["suffixes"]:
                        detected_types.add(project_type)
                        break  # No need to check other presets for this file

            # Check for exact matches (like docker-compose.yml)
            for project_type, preset in get_presets().items():
                if file in preset.get("include", []):
                    detected_types.add(project_type)

    logger.debug(f"Detected project types: {detected_types}")
    return detected_types


def parse_filter_patterns(filter_string: str) -> Dict[str, List[str]]:
    if not filter_string:
        return {"include": [], "exclude": []}

    patterns = filter_string.split()
    include_patterns = []
    exclude_patterns = []
    for pattern in patterns:
        if pattern.startswith("!"):
            exclude_patterns.append(pattern[1:])
        else:
            include_patterns.append(pattern)
    logger.debug(
        f"Parsed filter patterns - Include: {include_patterns}, Exclude: {exclude_patterns}"
    )
    return {"include": include_patterns, "exclude": exclude_patterns}


def combine_presets(
    preset_names: List[str], filter_patterns: Dict[str, List[str]]
) -> Dict[str, List[str]]:
    combined_preset = {"include": set(), "exclude": set(DEFAULT_EXCLUDE)}
    presets = get_presets()

    for preset_name in preset_names:
        if preset_name in presets:
            preset = presets[preset_name]
            combined_preset["include"].update(preset["include"])
            combined_preset["exclude"].update(preset["exclude"])
        else:
            logger.warning(f"Preset '{preset_name}' not found. Skipping.")

    # Add user-specified filter patterns
    combined_preset["include"].update(filter_patterns["include"])
    combined_preset["exclude"].update(filter_patterns["exclude"])

    logger.debug(
        f"Combined preset - Include: {combined_preset['include']}, Exclude: {combined_preset['exclude']}"
    )
    return {
        "include": sorted(list(combined_preset["include"])),
        "exclude": sorted(list(combined_preset["exclude"])),
    }


def read_gitignore(gitignore_path):
    patterns = []
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, "r") as f:
            patterns = f.read().splitlines()
    logger.debug(f"Read {len(patterns)} patterns from .gitignore")
    return patterns


def create_file_element(file_path, file_content):
    file_element = ET.Element("file", path=file_path)
    content_element = ET.SubElement(file_element, "content")
    content_element.text = file_content
    return file_element


def generate_xml(
    folder_path: str,
    include_patterns: List[str],
    exclude_patterns: List[str],
    gitignore_path: str,
    task: str,
    include_dotfiles: bool,
) -> str:
    gitignore_patterns = read_gitignore(gitignore_path)

    # Create separate PathSpec objects for include and exclude patterns
    include_spec = pathspec.PathSpec.from_lines("gitwildmatch", include_patterns)
    exclude_spec = pathspec.PathSpec.from_lines(
        "gitwildmatch", exclude_patterns + gitignore_patterns
    )

    root_element = ET.Element("root")
    project_context = ET.SubElement(root_element, "project_context")

    file_count = 0
    error_count = 0

    for root, dirs, files in os.walk(folder_path):
        rel_root = os.path.relpath(root, folder_path)
        for file in files:
            rel_file_path = os.path.join(rel_root, file)
            if include_spec.match_file(rel_file_path) and not exclude_spec.match_file(
                rel_file_path
            ):
                try:
                    with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                        file_content = f.read()
                    file_element = create_file_element(rel_file_path, file_content)
                    project_context.append(file_element)
                    file_count += 1
                except Exception as e:
                    error_element = ET.Element("error", path=rel_file_path)
                    error_element.text = str(e)
                    project_context.append(error_element)
                    error_count += 1
                    logger.error(f"Error processing file {rel_file_path}: {str(e)}")

    logger.info(f"Processed {file_count} files with {error_count} errors")

    # Generate complete directory structure
    dir_structure_element = ET.SubElement(project_context, "directory_structure")
    for root, dirs, files in os.walk(folder_path):
        # Skip dotfiles and directories unless include_dotfiles is True
        if not include_dotfiles:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            files = [f for f in files if not f.startswith(".")]

        rel_root = os.path.relpath(root, folder_path)
        if rel_root == ".":
            dir_element = dir_structure_element
        else:
            dir_element = ET.SubElement(
                dir_structure_element, "directory", path=rel_root
            )

        for file in files:
            rel_file_path = os.path.join(rel_root, file)
            ET.SubElement(dir_element, "file", path=rel_file_path)

    task_element = ET.SubElement(root_element, "task")
    task_element.text = task

    return ET.tostring(root_element, encoding="unicode", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description="Dump folder contents and structure.")
    parser.add_argument(
        "--list-versions",
        action="store_true",
        help="List all versions in the project history",
    )
    parser.add_argument(
        "--switch-version", type=str, help="Switch to a specific version (commit hash)"
    )
    parser.add_argument(
        "--create-branch", type=str, help="Create a new branch from the current version"
    )

    parser.add_argument(
        "folder_path", nargs="?", help="Path to the folder to be dumped"
    )
    parser.add_argument(
        "-o", "--output", help="Path to the output file (default: stdout)", default="-"
    )
    parser.add_argument(
        "--presets",
        nargs="+",
        choices=list(get_presets().keys()),
        help="Preset project types to combine (default: auto-detect)",
    )
    parser.add_argument(
        "--filter",
        type=str,
        help="Filter patterns to include or exclude files. Use '!' to exclude. Example: --filter '*.py !__pycache__ !*.pyc'",
    )
    parser.add_argument(
        "--include-dotfiles",
        action="store_true",
        help="Include dotfiles and folders in the output",
    )
    parser.add_argument(
        "--gitignore",
        help="Path to the .gitignore file (default: .gitignore in the folder_path)",
        default=None,
    )
    parser.add_argument(
        "--task",
        help="Task description to be included in the output",
        default="Describe this project in detail. Pay special attention to the structure of the code, the design of the project, any frameworks/UI frameworks used, and the overall structure/workflow. If artifacts are available, then display workflow and sequence diagrams to help describe the project.",
    )
    parser.add_argument(
        "--no-auto-detect",
        action="store_true",
        help="Disable auto-detection of project types",
    )
    parser.add_argument(
        "--view-presets",
        action="store_true",
        help="View all available presets",
    )
    parser.add_argument(
        "--save-presets",
        action="store_true",
        help="Save built-in presets to a YAML file",
    )
    parser.add_argument(
        "--analyze-deps",
        action="store_true",
        help="Analyze project dependencies",
        default=False,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Enter interactive mode after generating XML",
    )

    parser.add_argument(
        "--bedrock",
        action="store_true",
        help="Use AWS Bedrock for Claude API in interactive mode",
    )

    args = parser.parse_args()

    setup_logging(args.verbose)

    # Initialize version control
    version_control = initialize_version_control(args.folder_path)

    if args.view_presets:
        print(view_presets())
        return

    if args.save_presets:
        save_built_in_presets()
        logger.info("Built-in presets have been saved to ctxl_presets.yaml")
        return

    if not args.folder_path:
        logger.error(
            "folder_path is required unless --view-presets or --save-presets is specified"
        )
        parser.error(
            "folder_path is required unless --view-presets or --save-presets is specified"
        )

    # Handle version control CLI arguments
    if args.list_versions:
        history = version_control.get_version_history()
        for version in history:
            print(f"Commit: {version['id']}")
            print(f"Message: {version['message']}")
            print(f"Author: {version['author']}")
            print(f"Timestamp: {version['timestamp']}")
            print(f"Current: {'Yes' if version['is_current'] else 'No'}")
            print("---")
        return

    if args.switch_version:
        try:
            version_control.switch_to_version(args.switch_version)
            print(f"Switched to version: {args.switch_version}")
        except GitCommandError as e:
            print(f"Error switching version: {e}")
        return

    if args.create_branch:
        try:
            version_control.create_branch(args.create_branch)
            print(f"Created and switched to new branch: {args.create_branch}")
        except GitCommandError as e:
            print(f"Error creating branch: {e}")
        return

    # Load presets from the directory
    current_dir_presets = load_presets(
        os.path.join(args.folder_path, "ctxl_presets.yaml")
    )
    if current_dir_presets:
        logger.info("Loaded custom presets from the project directory.")

    # Auto-detect project types if not disabled and no presets specified
    if not args.no_auto_detect and not args.presets:
        detected_types = detect_project_types(args.folder_path)
        if detected_types:
            logger.info(f"Detected project types: {', '.join(detected_types)}")
            args.presets = list(detected_types)
        else:
            logger.info(
                "No specific project types detected. No presets will be applied."
            )
            args.presets = []
    elif not args.presets:
        args.presets = []

    # Parse filter patterns
    filter_patterns = parse_filter_patterns(args.filter or "")

    # Combine presets and apply filters
    combined_preset = combine_presets(args.presets, filter_patterns)

    # Remove the default dotfile exclude if --include-dotfiles is specified
    if args.include_dotfiles:
        combined_preset["exclude"] = [
            pattern for pattern in combined_preset["exclude"] if pattern != ".*"
        ]
        logger.debug("Included dotfiles in the output")

    # Set the default gitignore path if not provided
    if args.gitignore is None:
        args.gitignore = os.path.join(args.folder_path, ".gitignore")

    # Generate XML
    xml_output = generate_xml(
        args.folder_path,
        combined_preset["include"],
        combined_preset["exclude"],
        args.gitignore,
        args.task,
        args.include_dotfiles,
    )

    # Output XML
    if args.output == "-":
        output = sys.stdout
    else:
        output = args.output

    if isinstance(output, str):
        with open(output, "w", encoding="utf-8") as f:
            f.write(xml_output)
        logger.info(f"Output written to file: {output}")
    else:
        output.write(xml_output)
        logger.info("Output written to stdout")

    # Enter interactive mode if specified
    if args.interactive:
        if args.bedrock:
            chat_mode = ChatMode(bedrock=args.bedrock, version_control=version_control)
        else:
            chat_mode = ChatMode(bedrock=args.bedrock, version_control=version_control)
        chat_mode.start()


if __name__ == "__main__":
    main()
