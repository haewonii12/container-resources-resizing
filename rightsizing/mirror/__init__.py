"""GitOps mirror: keep the SET of apps in the manifest repo and ArgoCD equal to the Deployments in the cluster.

Existence only: new Deployments are added (git dir + manual-sync Application), removed ones are deleted after
guarded confirmation. The content of apps already in git is never changed (except never-synced mirror apps).
"""
