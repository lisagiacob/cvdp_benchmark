# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Commercial EDA Tool Support for CVDP Benchmark

Handles Docker network creation, image validation, and infrastructure setup
for commercial EDA tools like Cadence, Synopsys, and other verification platforms.
"""

import json
import logging
import re
import subprocess
from typing import Set, List, Optional, Dict, Any

from .config_manager import config
from .constants import VERIF_EDA_CATEGORIES, LICENSE_CONFIG

logger = logging.getLogger(__name__)

# Default timescale for modules without a `timescale directive (only applies to those)
XRUN_DEFAULT_TIMESCALE_OPTS = '-timescale 1ns/1ps'

# A `timescale=(...)` keyword argument, e.g. in runner.build(), outside comments
_RUNNER_TIMESCALE_RE = re.compile(r'^[^#\n]*\btimescale\s*=\s*\(', re.MULTILINE)


def check_docker_network_exists(network_name: str) -> bool:
    """
    Check if a Docker network exists.
    
    Args:
        network_name: Name of the Docker network to check
        
    Returns:
        True if network exists, False otherwise
    """
    try:
        result = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            check=True
        )
        existing_networks = result.stdout.strip().split('\n')
        return network_name in existing_networks
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to list Docker networks: {e}")
        return False
    except Exception as e:
        logger.error(f"Error checking Docker network existence: {e}")
        return False


def check_docker_image_exists(image_name: str) -> bool:
    """
    Check if a Docker image exists locally.
    
    Args:
        image_name: Name of the Docker image to check
        
    Returns:
        True if image exists locally, False otherwise
    """
    try:
        # Check if image exists locally
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            text=True,
            check=True
        )
        logger.debug(f"Docker image {image_name} exists locally")
        return True
    except subprocess.CalledProcessError:
        # Image doesn't exist locally, let Docker handle pulling during runtime
        logger.debug(f"Docker image {image_name} not found locally (will be pulled if available)")
        return False
    except Exception as e:
        logger.error(f"Error checking Docker image existence: {e}")
        return False


def create_license_network(network_name: str) -> bool:
    """
    Create a Docker network for EDA license server connectivity.
    
    Args:
        network_name: Name of the Docker network to create
        
    Returns:
        True if network was created successfully, False otherwise
    """
    try:
        logger.info(f"Creating Docker license network: {network_name}")
        subprocess.run(
            ["docker", "network", "create", network_name],
            capture_output=True,
            text=True,
            check=True
        )
        logger.info(f"Successfully created license network: {network_name}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to create Docker network {network_name}: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"Error creating Docker network {network_name}: {e}")
        return False


def get_dataset_categories(dataset_file: str) -> Set[int]:
    """
    Extract all categories present in a dataset file.
    
    Args:
        dataset_file: Path to the dataset JSON Lines file
        
    Returns:
        Set of category IDs found in the dataset
    """
    categories = set()
    
    try:
        with open(dataset_file, 'r') as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                    if 'categories' in data:
                        for cat in data['categories']:
                            # Only process categories that start with "cid"
                            if isinstance(cat, str) and cat.startswith('cid'):
                                try:
                                    # Extract numeric part from "cid###" format
                                    category_id = int(cat[3:])  # Remove "cid" prefix and convert to int
                                    categories.add(category_id)
                                except (ValueError, IndexError):
                                    # Skip invalid category formats
                                    continue
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning(f"Could not read dataset file {dataset_file}: {e}")
    
    return categories


def requires_commercial_eda_tools(dataset_file: str) -> bool:
    """
    Check if a dataset requires commercial EDA tool support.
    
    Uses two detection methods:
    1. Category-based: Check if dataset contains categories that require commercial EDA tools
    2. Template-based: Check if dataset contains __VERIF_EDA_IMAGE__ template variables
    
    Args:
        dataset_file: Path to the dataset JSON Lines file
        
    Returns:
        True if dataset requires commercial EDA tools (by category OR template detection)
    """
    # Method 1: Category-based detection
    dataset_categories = get_dataset_categories(dataset_file)
    eda_required_categories = set(LICENSE_CONFIG['LICENSE_REQUIRED_CATEGORIES'])
    
    category_requires_eda = bool(dataset_categories.intersection(eda_required_categories))
    
    # Method 2: Template-based detection - scan for __VERIF_EDA_IMAGE__
    template_requires_eda = _scan_for_eda_template_variables(dataset_file)
    
    requires_eda = category_requires_eda or template_requires_eda
    
    if requires_eda:
        reasons = []
        if category_requires_eda:
            matching_categories = sorted(dataset_categories.intersection(eda_required_categories))
            reasons.append(f"categories {matching_categories}")
        if template_requires_eda:
            reasons.append("__VERIF_EDA_IMAGE__ template variables")
        
        logger.info(f"Dataset requires commercial EDA tools due to: {', '.join(reasons)}")
    
    return requires_eda


def datapoint_requires_eda_license(datapoint: Dict[str, Any]) -> bool:
    """
    Determine if a specific datapoint requires commercial EDA license network.
    
    Uses the same detection logic as requires_commercial_eda_tools() but operates
    on a single datapoint rather than scanning the entire dataset file.
    
    Args:
        datapoint: Single datapoint dictionary from the dataset
        
    Returns:
        True if this datapoint requires EDA license network, False otherwise
    """
    try:
        # Method 1: Category-based detection
        eda_required_categories = set(LICENSE_CONFIG['LICENSE_REQUIRED_CATEGORIES'])
        categories = datapoint.get('categories', [])
        
        # Handle both string (cid###) and integer categories
        datapoint_categories = set()
        for cat in categories:
            if isinstance(cat, str) and cat.startswith('cid'):
                try:
                    category_id = int(cat[3:])  # Remove "cid" prefix and convert to int
                    datapoint_categories.add(category_id)
                except (ValueError, IndexError):
                    continue
            elif isinstance(cat, int):
                datapoint_categories.add(cat)
        
        if datapoint_categories.intersection(eda_required_categories):
            return True
        
        # Method 2: Template-based detection - check for EDA template variables in content
        datapoint_str = str(datapoint)
        eda_templates = ['__VERIF_EDA_IMAGE__', '__LICENSE_NETWORK__']
        if any(template in datapoint_str for template in eda_templates):
            return True
        
        return False
        
    except Exception as e:
        # If we can't determine, err on the side of caution and return False
        logger.warning(f"Could not determine EDA license requirement for datapoint: {e}")
        return False


def harness_runner_sets_timescale(service_config: Dict[str, Any], harness_files: Dict[str, str]) -> bool:
    """
    Tell whether a harness service runs a cocotb runner that is given a timescale.

    With cocotb 2 (NEW_HARNESS), Runner.build(timescale=...) passes -timescale to
    xrun itself; adding the XRUNOPTS default on top makes xrun fail with OPTNOML
    ("Multiple -timescale option specified"). cocotb 1.x ignored that argument on
    Xcelium, which is why the old harness relies on XRUNOPTS. The runner call may
    live in any harness Python file (often harness_library.py), so all are scanned.

    Args:
        service_config: Docker Compose service dictionary
        harness_files: Harness files of the datapoint, {path: content}

    Returns:
        True if the service runs a cocotb runner built with a timescale
    """
    if 'process.py' in str(service_config.get('command', '')):
        return False
    sources = [content for path, content in harness_files.items() if path.endswith('.py')]
    return (any('get_runner' in content for content in sources)
            and any(_RUNNER_TIMESCALE_RE.search(content) for content in sources))


def mount_cadence_tools_on_host_network(service_config: Dict[str, Any],
                                        default_timescale: bool = True) -> None:
    """
    Set network_mode: host and mount the Cadence tools read-only on one Docker Compose service.

    Host networking is needed to reach the FlexLM license server bound to
    localhost on the host machine (CDS_LIC_FILE=5280@localhost).

    XRUNOPTS gives xrun a default timescale: otherwise elaboration fails with
    CUMSTS when a testbench declares `timescale and the reference RTL doesn't.
    Skip it (default_timescale=False) when something else already passes
    -timescale, see harness_runner_sets_timescale().

    Args:
        service_config: Docker Compose service dictionary, modified in place
        default_timescale: Whether to set the XRUNOPTS default timescale
    """
    service_config.pop('networks', None)
    service_config['network_mode'] = 'host'

    volumes = service_config.get('volumes', [])
    for tool_path in (config.get('CDS_XCELIUM'), config.get('CDS_VMANAGER')):
        if tool_path:
            volumes.append(f'{tool_path}:{tool_path}:ro')
    service_config['volumes'] = volumes

    if not default_timescale:
        return

    # Compose accepts environment as a mapping or as a list of KEY=VALUE strings
    environment = service_config.get('environment') or {}
    if isinstance(environment, list):
        if not any(entry.startswith('XRUNOPTS=') for entry in environment):
            environment.append(f'XRUNOPTS={XRUN_DEFAULT_TIMESCALE_OPTS}')
    else:
        environment.setdefault('XRUNOPTS', XRUN_DEFAULT_TIMESCALE_OPTS)
    service_config['environment'] = environment


def _get_image_path_env(image_name: str) -> str:
    """
    Return the PATH baked into a Docker image, falling back to the Debian default.

    Args:
        image_name: Name of the Docker image to inspect

    Returns:
        The image's PATH value
    """
    default_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Env}}", image_name],
            capture_output=True,
            text=True,
            check=True
        )
        for entry in json.loads(result.stdout) or []:
            if entry.startswith("PATH="):
                return entry[len("PATH="):]
    except Exception as e:
        logger.warning(f"Could not read PATH from image {image_name}, using default: {e}")
    return default_path


def expose_cadence_tools_to_agent(service_config: Dict[str, Any], image_name: str) -> None:
    """
    Make Xcelium/Vmanager usable inside an agent Docker Compose service.

    Agent images don't ship the Cadence environment, so on top of the host
    network and mounts used by the harness this sets the same variables as
    docker/Dockerfile-cadence-verif. PATH is the image's own PATH with the
    Cadence bin directories appended, since setting it in Compose replaces it.

    Args:
        service_config: Docker Compose agent service dictionary, modified in place
        image_name: Agent Docker image, used to read its PATH
    """
    mount_cadence_tools_on_host_network(service_config)

    environment = service_config.setdefault('environment', {})
    bin_dirs = []

    xcelium_path = config.get('CDS_XCELIUM')
    if xcelium_path:
        environment['CDS_XCELIUM'] = xcelium_path
        bin_dirs += [f'{xcelium_path}/bin', f'{xcelium_path}/tools/bin/64bit', f'{xcelium_path}/tools/bin']

    vmanager_path = config.get('CDS_VMANAGER')
    if vmanager_path:
        environment['CDS_VMANAGER'] = vmanager_path
        bin_dirs.append(f'{vmanager_path}/bin')

    environment['PATH'] = ':'.join([_get_image_path_env(image_name)] + bin_dirs)
    environment['CDS_LIC_FILE'] = '5280@localhost'
    environment['LM_LICENSE_FILE'] = '5280@localhost'
    environment['CDS_SKIP_OS_CHECK_ON_STARTUP'] = '1'


def _scan_for_eda_template_variables(dataset_file: str) -> bool:
    """
    Scan dataset file for EDA template variables that indicate commercial tool requirements.
    
    Args:
        dataset_file: Path to the dataset JSON Lines file
        
    Returns:
        True if dataset contains __VERIF_EDA_IMAGE__ template variables
    """
    try:
        with open(dataset_file, 'r', encoding='utf-8') as f:
            # Read the entire file content to search for template variables
            content = f.read()
            
            # Check for commercial EDA template variables
            eda_templates = ['__VERIF_EDA_IMAGE__', '__LICENSE_NETWORK__']
            
            for template in eda_templates:
                if template in content:
                    logger.debug(f"Found EDA template variable {template} in {dataset_file}")
                    return True
            
            return False
            
    except Exception as e:
        logger.warning(f"Could not scan dataset file {dataset_file} for EDA templates: {e}")
        return False


def validate_commercial_eda_setup(dataset_file: str) -> Dict[str, Any]:
    """
    Validate commercial EDA tool setup for a given dataset.
    
    Args:
        dataset_file: Path to the dataset JSON Lines file
        
    Returns:
        Dictionary with validation results including:
        - required: Whether commercial EDA tools are required
        - network_name: Configured license network name
        - network_exists: Whether the license network exists
        - auto_created: Whether the license network was auto-created during validation
        - verif_image: Configured verification image
        - verif_image_exists: Whether the verification image exists locally
        - validation_passed: Overall validation status
        - warnings: List of warning messages
        - errors: List of error messages
    """
    result = {
        'required': False,
        'network_name': None,
        'network_exists': False,
        'auto_created': False,
        'verif_image': None,
        'verif_image_exists': False,
        'validation_passed': True,
        'warnings': [],
        'errors': []
    }
    
    # Check if commercial EDA tools are required for this dataset
    if not requires_commercial_eda_tools(dataset_file):
        result['required'] = False
        logger.debug("Dataset does not require commercial EDA tool support")
        return result
    
    result['required'] = True
    
    # Get license network configuration
    network_name = config.get('LICENSE_NETWORK')
    auto_create = config.get('LICENSE_NETWORK_AUTO_CREATE')
    verif_image = config.get('VERIF_EDA_IMAGE')
    
    result['network_name'] = network_name
    result['verif_image'] = verif_image
    
    # Check if verification image is configured and exists
    if not verif_image:
        result['errors'].append(
            "VERIF_EDA_IMAGE not configured. Commercial EDA tools require a verification image with EDA tools."
        )
        result['validation_passed'] = False
    else:
        # Check if the verification image exists locally
        verif_image_exists = check_docker_image_exists(verif_image)
        result['verif_image_exists'] = verif_image_exists
        
        if not verif_image_exists:
            result['errors'].append(
                f"Verification image '{verif_image}' not found locally. "
                f"Commercial EDA tools require the image to be available before execution. "
                f"Build or pull the image first."
            )
            result['validation_passed'] = False
    
    # Check if license network exists
    network_exists = check_docker_network_exists(network_name)
    result['network_exists'] = network_exists
    
    if not network_exists:
        if auto_create:
            logger.info(f"License network '{network_name}' does not exist. Attempting to create it...")
            if create_license_network(network_name):
                result['network_exists'] = True
                result['auto_created'] = True  # Mark that we auto-created it
                logger.info(f"Successfully created license network: {network_name}")
                
                # Register cleanup for auto-created license network
                import atexit
                def cleanup_auto_created_license_network():
                    logger.info(f"Cleaning up auto-created license network: {network_name}")
                    remove_license_network(network_name)
                
                atexit.register(cleanup_auto_created_license_network)
            else:
                result['errors'].append(f"Failed to create license network: {network_name}")
                result['validation_passed'] = False
        else:
            result['errors'].append(
                f"License network '{network_name}' does not exist and auto-creation is disabled. "
                f"Create it manually with: docker network create {network_name}"
            )
            result['validation_passed'] = False
    
    return result


def print_commercial_eda_info(validation_result: Dict[str, Any]) -> None:
    """
    Print commercial EDA tool validation information to the console.
    
    Args:
        validation_result: Result from validate_commercial_eda_setup()
    """
    if not validation_result['required']:
        return
    
    print("\n" + "="*60)
    print("COMMERCIAL EDA TOOL VALIDATION")
    print("="*60)
    
    print(f"License Network: {validation_result['network_name']}")
    print(f"Network Exists: {'✓' if validation_result['network_exists'] else '✗'}")
    print(f"Verification Image: {validation_result['verif_image'] or 'Not configured'}")
    if validation_result['verif_image']:
        print(f"Image Exists Locally: {'✓' if validation_result['verif_image_exists'] else '✗'}")
    print(f"Validation Status: {'✓ PASSED' if validation_result['validation_passed'] else '✗ FAILED'}")
    
    if validation_result['warnings']:
        print("\nWarnings:")
        for warning in validation_result['warnings']:
            print(f"  ⚠ {warning}")
    
    if validation_result['errors']:
        print("\nErrors:")
        for error in validation_result['errors']:
            print(f"  ✗ {error}")
    
    if validation_result['validation_passed']:
        print("\n✓ Commercial EDA tool setup is ready for execution.")
    else:
        print("\n✗ Commercial EDA tool setup has issues that need to be resolved.")
        print("   Please address the errors above before running EDA tool workflows.")
    
    print("="*60)


def get_commercial_eda_docker_args(dataset_file: str) -> List[str]:
    """
    Get Docker arguments for commercial EDA tool connectivity.
    
    Args:
        dataset_file: Path to the dataset JSON Lines file
        
    Returns:
        List of Docker arguments to add license network connectivity
    """
    if not requires_commercial_eda_tools(dataset_file):
        return []
    
    network_name = config.get('LICENSE_NETWORK')
    
    # Validate that network exists before returning arguments
    if not check_docker_network_exists(network_name):
        logger.warning(f"License network '{network_name}' does not exist. Docker containers may not have license access.")
        return []
    
    return ["--network", network_name]


def remove_license_network(network_name: str) -> bool:
    """
    Remove an auto-created license network.
    
    This function removes license networks that were auto-created during the benchmark run.
    It follows the same pattern as remove_docker_network() but with additional logging
    for license network operations.
    
    Args:
        network_name: Name of the license network to remove
        
    Returns:
        True if network was removed successfully or didn't exist, False otherwise
    """
    try:
        # Check if network exists (using same pattern as network_util.remove_docker_network)
        result = subprocess.run(
            f"docker network ls --filter name=^{network_name}$ --format '{{{{.Name}}}}'",
            shell=True, capture_output=True, text=True
        )
        
        if result.stdout.strip() != network_name:
            logger.debug(f"License network '{network_name}' does not exist, nothing to remove")
            return True
            
        # Remove the network (Docker will fail if containers are using it, which is expected behavior)
        logger.info(f"Removing auto-created license network '{network_name}'")
        result = subprocess.run(
            f"docker network rm {network_name}",
            shell=True, capture_output=True, text=True
        )
        
        if result.returncode == 0:
            logger.info(f"Successfully removed auto-created license network: {network_name}")
            return True
        else:
            # Log the error but don't treat it as a critical failure
            # This is expected if containers are still using the network
            logger.warning(f"Could not remove auto-created license network '{network_name}': {result.stderr.strip()}")
            return False
            
    except Exception as e:
        logger.error(f"Error removing auto-created license network '{network_name}': {e}")
        return False 