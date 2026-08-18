param(
    [string]$Image = "n8nio/n8n:2.6.4"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$n8nRoot = Split-Path -Parent $PSScriptRoot
$runId = "dayz_n8n_" + ([guid]::NewGuid().ToString("N").Substring(0, 10))
$volumeName = "${runId}_data"
$containerName = "${runId}_app"
$rootForDocker = $n8nRoot.Replace("\", "/")
$started = $false

try {
    docker volume create $volumeName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create validation volume" }

    docker run --rm --network none `
        -e N8N_DIAGNOSTICS_ENABLED=false `
        -e N8N_VERSION_NOTIFICATIONS_ENABLED=false `
        -v "${volumeName}:/home/node/.n8n" `
        -v "${rootForDocker}/workflows:/imports:ro" `
        $Image import:workflow --separate --input=/imports
    if ($LASTEXITCODE -ne 0) { throw "Workflow import failed" }

    docker run -d --name $containerName --network none `
        -e N8N_DIAGNOSTICS_ENABLED=false `
        -e N8N_VERSION_NOTIFICATIONS_ENABLED=false `
        -e N8N_SECURE_COOKIE=false `
        -e N8N_LOG_LEVEL=warn `
        -v "${volumeName}:/home/node/.n8n" `
        -v "${rootForDocker}/tests:/validation:ro" `
        $Image start | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not start validation n8n" }
    $started = $true

    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        docker exec $containerName node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "Validation n8n did not become healthy" }

    docker exec $containerName node /validation/validate_runtime.mjs
    if ($LASTEXITCODE -ne 0) { throw "Runtime bootstrap validation failed" }
}
catch {
    if ($started) {
        docker logs --tail 200 $containerName
    }
    throw
}
finally {
    docker rm -f $containerName 2>$null | Out-Null
    docker volume rm $volumeName 2>$null | Out-Null
}
