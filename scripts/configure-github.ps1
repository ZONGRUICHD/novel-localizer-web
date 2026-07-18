[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Repository = "ZONGRUICHD/novel-localizer-web",
    [switch]$IncludeCloudflarePagesCheck
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Assert-GitHubCliSuccess {
    param([string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub CLI failed while $Operation (exit code $LASTEXITCODE)."
    }
}

function Invoke-GitHubJsonRequest {
    param(
        [string]$Payload,
        [string]$Operation,
        [string[]]$Arguments
    )

    $inputFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText(
            $inputFile,
            $Payload,
            [System.Text.UTF8Encoding]::new($false)
        )
        & gh api @Arguments --input $inputFile | Out-Null
        Assert-GitHubCliSuccess $Operation
    }
    finally {
        [System.IO.File]::Delete($inputFile)
    }
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) is required."
}

gh auth status | Out-Null
Assert-GitHubCliSuccess "checking GitHub authentication"

$checks = @(
    "Repository policy",
    "Backend",
    "Frontend and edge",
    "OpenAPI client drift",
    "Dependency audit",
    "Security scan (javascript-typescript)",
    "Security scan (python)"
)
if ($IncludeCloudflarePagesCheck) {
    $checks += "Cloudflare Pages"
}

$repoSettings = @{
    has_issues                = $true
    has_projects              = $false
    has_wiki                  = $false
    delete_branch_on_merge    = $true
    allow_merge_commit        = $false
    allow_rebase_merge        = $false
    allow_squash_merge        = $true
    allow_update_branch       = $true
} | ConvertTo-Json -Depth 5

$branchProtection = @{
    required_status_checks = @{
        strict   = $true
        contexts = $checks
    }
    enforce_admins = $true
    required_pull_request_reviews = @{
        dismiss_stale_reviews           = $true
        require_code_owner_reviews      = $false
        required_approving_review_count = 0
    }
    restrictions                     = $null
    required_conversation_resolution = $true
    required_linear_history          = $true
    allow_force_pushes                = $false
    allow_deletions                   = $false
    block_creations                   = $false
    required_signatures               = $false
} | ConvertTo-Json -Depth 8

if ($PSCmdlet.ShouldProcess($Repository, "configure private repository settings")) {
    Invoke-GitHubJsonRequest $repoSettings "configuring repository settings" @(
        "--method", "PATCH", "repos/$Repository"
    )
    $isPrivate = gh api "repos/$Repository" --jq .private
    Assert-GitHubCliSuccess "checking repository visibility"
    if ($isPrivate -ne "true") {
        throw "Refusing to configure a repository that is not private."
    }
}

if ($PSCmdlet.ShouldProcess("$Repository main", "enable branch protection")) {
    Invoke-GitHubJsonRequest $branchProtection "enabling branch protection" @(
        "--method", "PUT", "repos/$Repository/branches/main/protection"
    )
}

if ($WhatIfPreference) {
    Write-Output "GitHub repository policy dry run completed for $Repository"
    return
}

$ownerId = gh api users/ZONGRUICHD --jq .id
Assert-GitHubCliSuccess "reading owner identity"
$environment = @{
    wait_timer                    = 0
    prevent_self_review           = $false
    reviewers                     = @(@{ type = "User"; id = [int64]$ownerId })
    deployment_branch_policy      = @{
        protected_branches     = $true
        custom_branch_policies = $false
    }
} | ConvertTo-Json -Depth 6

if ($PSCmdlet.ShouldProcess("$Repository production-server", "configure protected deployment environment")) {
    Invoke-GitHubJsonRequest $environment "configuring the production deployment environment" @(
        "--method", "PUT", "repos/$Repository/environments/production-server"
    )
}

Write-Output "GitHub repository policy configured for $Repository"
if (-not $IncludeCloudflarePagesCheck) {
    Write-Warning "Cloudflare Pages is not yet a required check. Re-run with -IncludeCloudflarePagesCheck after the GitHub App has emitted its first check run."
}
