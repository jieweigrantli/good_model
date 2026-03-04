"""Patch EV_Charging_Marginal_Emissions.ipynb to use a copy of WEC.json instead of modifying the original."""
import sys

nb_path = "EV_Charging_Marginal_Emissions.ipynb"

try:
    with open(nb_path, "r", encoding="utf-8") as f:
        content = f.read()
except Exception as e:
    print(f"Error reading: {e}")
    sys.exit(1)

# In notebook JSON, each line is a separate string with \\n at end. Match the raw format.
# Fix 1: Replace first WEC_FILE assignment with copy logic
old1 = '        "WEC_FILE = \'Examples/WEC.json\'\\n",\n        "\\n",\n        "with open(WEC_FILE, \'r\') as f:\\n",'
new1 = '        "# Use a COPY - do not modify the original WEC.json\\n",\n        "WEC_FILE_ORIGINAL = \'Examples/WEC.json\'\\n",\n        "WEC_FILE = \'Examples/WEC_modified.json\'\\n",\n        "\\n",\n        "import shutil\\n",\n        "if os.path.exists(WEC_FILE_ORIGINAL):\\n",\n        "    shutil.copy2(WEC_FILE_ORIGINAL, WEC_FILE)\\n",\n        "    print(f\\\\\\"Created working copy: {WEC_FILE} (original preserved)\\\\\")\\n",\n        "\\n",\n        "with open(WEC_FILE, \'r\') as f:\\n",'

if old1 in content:
    content = content.replace(old1, new1)
    print("Applied fix 1: WEC copy setup")
else:
    print("Fix 1 not found")
    # Try alternative - maybe different spacing
    if 'WEC_FILE = \'Examples/WEC.json\'' in content:
        print("  (WEC_FILE assignment exists, trying simpler replace)")
        content = content.replace(
            '"WEC_FILE = \'Examples/WEC.json\'\\n",',
            '"WEC_FILE_ORIGINAL = \'Examples/WEC.json\'\\n",\n        "WEC_FILE = \'Examples/WEC_modified.json\'\\n",\n        "import shutil\\n",\n        "if os.path.exists(WEC_FILE_ORIGINAL):\\n",\n        "    shutil.copy2(WEC_FILE_ORIGINAL, WEC_FILE)\\n",\n        "    print(f\\"Created working copy (original preserved)\\")\\n",',
            1  # Only first occurrence
        )
        print("  Applied simpler fix 1")

# Fix 2: Second WEC_FILE (in the modify section) - remove it since WEC_FILE is already set
old2 = '        "WEC_FILE = \'Examples/WEC.json\'\\n",\n        "# Modify nuclear prices'
new2 = '        "# Modify nuclear prices'
if old2 in content:
    content = content.replace(old2, new2)
    print("Applied fix 2: Removed redundant WEC_FILE")
else:
    print("Fix 2 not found")

# Fix 3: GRAPH_FILE (format in notebook: "GRAPH_FILE = 'Examples/WEC.json'  # Path...")
old3 = "GRAPH_FILE = 'Examples/WEC.json'"
new3 = "GRAPH_FILE = 'Examples/WEC_modified.json'"
# Try both formats (with/without comment)
content = content.replace("GRAPH_FILE = 'Examples/WEC.json'  # Path to graph JSON file", 
                         "GRAPH_FILE = 'Examples/WEC_modified.json'  # Path to graph JSON file")
if old3 in content and new3 not in content:
    content = content.replace(old3, new3)
    print("Applied fix 3: GRAPH_FILE uses modified copy")
else:
    print("Fix 3: GRAPH_FILE already correct or applied")

# Remove backup block - no longer needed
old_backup = '    if os.path.exists(WEC_FILE):\n        backup_path = WEC_FILE + \'.bak\''
if old_backup in content:
    # Remove the backup block - replace with nothing (we're not overwriting original)
    content = content.replace(
        '    if os.path.exists(WEC_FILE):\n        backup_path = WEC_FILE + \'.bak\'\n        shutil.copy2(WEC_FILE, backup_path)\n        print(f"  Backed up original to {backup_path}")\n    \n    ',
        '    '
    )
    print("Removed redundant backup block")
else:
    print("Backup block not found or different format")

with open(nb_path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")
