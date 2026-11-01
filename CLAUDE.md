# my-netops-platform

## 목적
- 어느 회사든 clone → ./install.sh → sites/<회사>.yaml 작성으로 끝나는 개인 NetOps 플랫폼
- 회사가 바뀌어도 코드는 고치지 않고 사이트 프로필만 새로 만든다
- 단일 저장소, 모듈은 디렉터리로 나눈다. 버전 태그는 저장소 하나에 하나

## 절대 규칙
- .env, sites/ 아래 SITE-EXAMPLE.yaml 외 파일은 읽지도 출력하지도 않는다
- 비밀번호·토큰을 출력이나 로그에 남기지 않는다. 확인이 필요하면 길이만 출력한다
- 회사명, 실제 장비 IP, 계정, config는 코드·예시·커밋에 넣지 않는다. 예시는 가명·사설 IP만
- 운영망 대상 코드는 읽기 전용(show, 읽기 API)만
- docker compose down -v, git tag 생성, 강제 push는 하지 않는다 (사용자가 직접)

## 코드 규칙
- Python 3.12, 표준 라이브러리(urllib, json) + python3-yaml(apt)만 사용. pip 패키지 추가 금지
- PEP 8, 타입 힌트, print 대신 logging, CLI는 argparse
- 연결 예외는 OSError로 처리
- Zabbix API는 실패해도 HTTP 200. body의 error 키로 판단
- create는 멱등이 아니므로 항상 get(filter) 먼저. 모든 스크립트는 두 번 실행해도 결과가 같아야 한다
- Zabbix ID(groupid, templateid)는 고정하지 않고 이름으로 조회한다 (설치마다 다름)
- Zabbix가 주는 숫자 값은 문자열("2")이다. int로 바꿔서 비교한다
- YAML은 yaml.safe_load만 사용
- 이미지 버전은 compose.yaml에 고정된 값만. latest 금지
- 문서(README, docs)에 이모지 금지. H1/H2/H3 + 불릿 + 코드블록

## 작업 방식
- 한 번에 하나씩. 변경 → 검증 → 커밋
- 수정 전에 계획을 먼저 보여주고 승인을 받는다
- 확정된 설계(docs/lessons/, 아래 inventory 동작)와 다르게 하려면 먼저 묻는다
- 동작에 영향이 있는 변경 뒤에는 ./install.sh 재실행으로 "변경 없음"을 확인한다
- 커밋: Conventional Commits 타입(영어) + 설명(한글, 쉬운 말)
- 안내하는 실행 명령에 <...> 같은 자리표시를 넣지 않는다

## 현재 위치: v0.2 inventory (Lesson 1-1)
1. scripts/zabbix_api.py 분리: bootstrap.py에서 ApiError, read_env, ZabbixClient,
   wait_for_zabbix, login_admin, ensure_hostgroup + 관련 상수 이동. 동작 변화 없음
2. scripts/inventory.py: 프로필 검사 → 조회 → 변경 목록 → --dry-run 또는 실행 → 요약 → 권한 확인
3. down -v 후 처음부터 검증 → docs/lessons/1-1.md → v0.2 태그

## 사이트 프로필 (sites/<회사>.yaml)
- 필드: company / sites[].code / sites[].devices[].name / devices[].ip 만
- code → Zabbix 그룹 Sites/<code>. 파일 안 유일, '/' 금지
- name → Zabbix 호스트 이름 그대로. 파일 전체 유일, 영문·숫자·. - _ 만
- ip: IPv4만, 파일 전체 유일
- 모르는 키, 중복 키, 문자열이 아닌 값(YAML 1.1의 NO/ON → bool, 0으로 시작 → 숫자)은 에러
- 검사는 API 호출 전에 전부. 하나라도 틀리면 아무것도 바꾸지 않고 종료

## inventory 동작
- 관리 범위: hostgroup.get search "Sites/" + startSearch (상위 Sites 자체 제외)
- 조회: host.get 1회 (groupids + selectInterfaces / selectHostGroups / selectParentTemplates)
- 그룹이 없으면 host.get을 호출하지 않는다 (groupids 키가 빠지면 전체 호스트가 온다)
- 비활성 대상은 응답에 Sites/* 그룹이 실제로 붙은 호스트만
- 그룹 없음 → hostgroup.create
- 호스트 없음 → host.create (ICMP Ping 템플릿 + agent 인터페이스 type 1, port 10050 + 그룹)
- IP 다름 → hostinterface.update (삭제 후 재생성 금지, 이력 유지)
- 거점 바뀜 → host.update groups
- 템플릿 없음 → host.massadd
- 프로필에 있는데 비활성 → host.update status 0
- Sites/*에 있는데 프로필에 없음 → host.update status 1 (삭제 안 함)
- 같은 이름이 Sites/* 밖에 있음 → 에러
- 마지막에 "생성 n / 수정 n / 비활성 n / 변경 없음 n" + grafana 그룹이 모든 Sites/*를 읽는지 확인

## 역할 분담
- Claude Code: 코드 작성·수정, 검증 명령 실행, 커밋 메시지 작성
- 사용자: plan 승인, 커밋 승인(승인되면 Claude Code가 이어서 push), 설계 결정, 운영망 관련 판단
- 설계 결정은 사용자와 별도로 정한다. 정해지지 않은 설계는 코드로 먼저 정하지 말고 묻는다

## 작업 규칙
- 파일 수정은 Edit 도구로 한다. python3 - <<EOF, sed 등 셸로 파일을 다시 쓰지 않는다
- 큰 블록 삭제는 블록마다 따로 Edit한다
- git add는 파일을 지정한다. git add -A, git add . 금지
- 커밋은 git commit과 git push를 한 명령으로 묶어 승인 한 번에 끝낸다
- 검증 순서: py_compile → --help(실제 import 확인) → 동작 확인(install.sh 또는 대상 스크립트)
- 테스트용 파일은 /tmp에 만든다. sites/ 아래에 만들지 않는다
- 커밋 전에 reviewer 서브에이전트로 diff를 검토받는다

## 결정 처리
- 설계(CLAUDE.md, docs/)에 없는 결정이 필요하면 구현을 멈추고 사용자에게 선택지와 추천을 묻는다
- 사용자가 결정하면 docs/DECISIONS.md 맨 아래에 날짜, 결정, 이유를 한 줄씩 추가하고 같은 커밋에 넣는다
