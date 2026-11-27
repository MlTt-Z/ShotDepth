param([string]$ComfyUrl = 'http://127.0.0.1:8188')

$ErrorActionPreference = 'Stop'
foreach ($shot in 'A', 'B') {
    foreach ($condition in 'depth', 'no_depth') {
        $graph = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'workflow_api.json') | ConvertFrom-Json
        $graph.'13'.inputs.filename_prefix = "spikemark-g0/$shot/$condition"
        if ($condition -eq 'depth') {
            $graph.'6'.inputs.image = "spikemark-g0/$shot/vace_depth.png"
        } else {
            $graph.'8'.inputs.PSObject.Properties.Remove('control_video')
            $graph.PSObject.Properties.Remove('6')
            $graph.PSObject.Properties.Remove('7')
        }
        $request = @{ prompt = $graph } | ConvertTo-Json -Depth 20 -Compress
        $job = Invoke-RestMethod -Method Post -Uri "$ComfyUrl/prompt" -ContentType 'application/json' -Body $request
        [pscustomobject]@{ Shot = $shot; Condition = $condition; PromptId = $job.prompt_id }
    }
}
