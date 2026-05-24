# GitHub 배포 절차

## 1. GitHub 로그인

PowerShell에서 한 번만 실행합니다.

```powershell
gh auth login -h github.com
```

브라우저 로그인을 완료합니다.

## 2. 원격 저장소 만들고 업로드

아래 명령은 공개 저장소를 만듭니다. 무료 GitHub Pages와 Actions를 쓰기 가장 쉽습니다.

```powershell
gh repo create quant-guardian --public --source . --remote origin --push
```

비공개 저장소로 만들고 싶으면 `--public` 대신 `--private`를 쓰면 됩니다. 다만 GitHub Pages 공개 배포 조건은 계정/플랜에 따라 달라질 수 있습니다.

## 3. GitHub Pages 켜기

GitHub 저장소에서:

```text
Settings -> Pages -> Source: GitHub Actions
```

## 4. 첫 배포 실행

GitHub 저장소에서:

```text
Actions -> Build and Deploy Quant Guardian -> Run workflow
```

완료되면 `https://계정명.github.io/quant-guardian/` 주소로 접속합니다.

## 5. 자동 갱신

워크플로는 평일 07:30 KST에 자동 실행됩니다. 미국 정규장 마감 이후의 데이터를 반영하기 위한 시간입니다.
