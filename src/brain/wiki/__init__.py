"""Wiki build pipeline: atomic blue/green static site swaps."""

# Pinned Quartz commit SHA. Bumping this is a code change, not a
# runtime gamble — the overlay tree is tested against this exact commit.
QUARTZ_PINNED_COMMIT = "d25a6eabf96751ffca56f8a8139272def7a65041"
QUARTZ_REPO_URL = "https://github.com/jackyzha0/quartz.git"
