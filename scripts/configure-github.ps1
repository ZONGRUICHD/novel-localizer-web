[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Repository = "ZONGRUICHD/novel-localizer-web",
    [switch]$IncludeCloudflarePagesCheck
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) is required."
}

gh auth status | Out-Null

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
    private                   = $true
    has_issues                = $true
    has_projects              = $false
    has_wiki                  = $false
    delete_branch_on_merge    = $true
    allow_merge_commit        = $false
    allow_rebase_merge        = $false
    allow_squash_merge        = $true
    allow_auto_merge          = $true
    allow_update_branch       = $true
    use_squash_pr_title_as_default = $true
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
    $repoSettings | gh api --method PATCH "repos/$Repository" --input - | Out-Null
}

if ($PSCmdlet.ShouldProcess("$Repository main", "enable branch protection")) {
    $branchProtection | gh api --method PUT "repos/$Repository/branches/main/protection" --input - | Out-Null
}

$ownerId = gh api users/ZONGRUICHD --jq .id
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
    $environment | gh api --method PUT "repos/$Repository/environments/production-server" --input - | Out-Null
}

Write-Output "GitHub repository policy configured for $Repository"
if (-not $IncludeCloudflarePagesCheck) {
    Write-Warning "Cloudflare Pages is not yet a required check. Re-run with -IncludeCloudflarePagesCheck after the GitHub App has emitted its first check run."
}
