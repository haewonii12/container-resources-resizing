# resource

ArgoCD(수동 sync)가 배포하는 manifest repo입니다. rightsizing agent가 이 repo의 `resources` 값을 조정하는 commit을 올립니다.

- 경로 규칙: `<namespace>/<app>/` (plain YAML). 앱 하나에 ArgoCD Application 하나를 둡니다.
- 승인: ArgoCD에서 diff를 확인하고 Sync합니다. 거절: 해당 commit을 `git revert`합니다.
- `sample-web`, `sample-api`는 흐름을 검증하기 위한 샘플입니다. request를 일부러 크게 잡아 두었습니다.
