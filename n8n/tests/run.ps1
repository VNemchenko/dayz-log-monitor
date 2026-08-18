$ErrorActionPreference = 'Stop'

$bundleRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Push-Location $bundleRoot
try {
    node .\tools\build_workflows.js
    python .\tests\validate_bundle.py
    node .\tests\test_workflow_code.js
}
finally {
    Pop-Location
}
