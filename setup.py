from cx_Freeze import setup, Executable

# Dependencies are automatically detected, but it might need fine tuning.
build_exe_options = {
    "packages": ["os", "pyodbc", "requests", "json", "decimal", "logging", "keyring", "keyrings.alt"],
    "excludes": ["tkinter"]
}

setup(
    name = "my_app_name",
    version = "0.1",
    description = "My app description",
    options = {"build_exe": build_exe_options},
    executables = [Executable("main.py", base=None)]
)
