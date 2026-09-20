# whisky-watch

스프링뱅크(로컬발리 포함), 헤이즐번, 롱로우, 키커런과 고숙성/희귀 라인업을 해외 샵에서 주기적으로 확인하고,
**상품가 + 한국행 배송비 + 한국 수입 세금**을 합친 도착가가 그 상품의 평균(중앙값) 가격보다 충분히 낮으면 텔레그램으로 알려줍니다.
GitHub Actions 에서 돌아가므로 PC를 켜 둘 필요가 없습니다.

## 도착가 계산

1. 샵 표시가에서 현지 VAT를 뺍니다(한국 수출은 VAT 면제). 샵별 `strip_vat`
2. 환율(ECB 기준, frankfurter.dev → open.er-api.com 폴백)로 원화 환산, 카드 수수료가 있으면 `fx_card_markup_pct` 가산
3. 한국 세금(관세청 예상세액 조회와 같은 순서)
   - 관세 = 과세가격 × 20%
   - 주세 = (과세가격 + 관세) × 72%
   - 교육세 = 주세 × 30%
   - 부가세 = (과세가격 + 관세 + 주세 + 교육세) × 10%
   - 과세가격: 물품가 150달러 이하는 물품가만, 초과는 물품가 + 한국행 운임
   - 1L 이하 1병·물품가 150달러 이하는 소액면세로 **관세·부가세만 면제**, 주세·교육세는 부과

`python -m whiskywatch tax --price 120 --currency GBP --strip-vat 0.2 --shipping 30` 으로 임의의 가격을 계산해 볼 수 있으니
[관세청 예상세액 조회](https://www.customs.go.kr/kcs/ad/tax/BuyTaxCalculation.do) 결과와 한 번 맞춰 보세요.

## 기준가(평균)는 어떻게 잡나

- 같은 상품(증류소·숙성·에디션 토큰·연도·배치·용량이 같은 키)의 **(샵·상품·일) 단위 최저 도착가**를 모아 최근 180일 **중앙값**을 기준가로 씁니다.
- 연도/배치까지 같은 이력이 부족하면 연도/배치를 뺀 같은 계열로 비교합니다(예: 2024 릴리스 이력으로 2026 릴리스 판단).
- 표본이 `min_samples`(기본 6) 미만이면 기준가가 없어서 알림이 나가지 않습니다. 처음 며칠은 이력을 쌓는 기간이고,
  급하면 `config.yaml` 의 `seed_prices` 에 직접 도착가(원)를 적어 두면 바로 비교합니다.
- 기본 임계값은 기준가 대비 **5% 이상** 저렴할 때입니다(`alert.discount_pct: 0` 이면 중앙값보다 조금이라도 싸면 알림).
- 같은 상품은 48시간 안에 다시 알리지 않고, 마지막 알림가보다 2% 이상 더 내려가면 다시 알립니다. 품절 상품은 알리지 않습니다.

## 설정 순서 (약 15분)

1. **텔레그램 봇**: 텔레그램에서 `@BotFather` → `/newbot` → 토큰 복사. 만든 봇에게 아무 메시지나 보낸 뒤
   `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어 `"chat":{"id": ...}` 숫자를 확인합니다.
2. **GitHub 저장소**: 이 폴더를 새 저장소에 올립니다.
   비공개는 무료 실행 시간이 월 2,000분이라 30분 간격이 한계이고, 공개는 무제한입니다(공개해도 토큰은 Secrets 라 노출되지 않고, 공개되는 것은 가격 이력과 설정뿐).
3. **Secrets**: 저장소 Settings → Secrets and variables → Actions 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록.
4. **점검 실행**: Actions 탭 → `setup-check` → Run workflow. 텔레그램 테스트 메시지가 오는지, 샵별로 상품이 몇 건 파싱되는지 로그를 봅니다.
   샵은 접속 IP 를 가려 받기도 해서 반드시 **GitHub 러너에서** 확인해야 합니다.
5. `search_url` 이 틀렸거나 파싱이 0건인 샵은 `config.yaml` 을 고치고 4번을 반복합니다.
6. 이후 `whisky-watch` 워크플로가 30분마다 자동 실행됩니다(수동 실행 시 dry_run 옵션 있음).

로컬에서 확인하려면:

```bash
pip install -r requirements.txt
python -m whiskywatch diagnose                 # 샵별 수집/파싱 점검
python -m whiskywatch run --dry-run            # 저장·발송 없이 알림 미리보기
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python -m whiskywatch test-telegram
python -m pytest tests                         # 테스트
```

## 자주 바꾸는 설정 (`config.yaml`)

| 하고 싶은 것 | 위치 |
| --- | --- |
| 감시 제품 추가/삭제 | `watchlist` 에 키워드 한 줄 (모든 단어가 상품명에 있으면 대상, 검색어로도 사용) |
| 알림 민감도 | `alert.discount_pct`, `alert.min_samples` |
| 관세 0% (FTA 원산지증명서 확보 시) | `tax.tariff_rate: 0` |
| 실제 배송비 반영 | 샵별 `ship_base`, `ship_extra` |
| 카드 해외결제 수수료 반영 | `fx_card_markup_pct` |
| 2병 이상 주문 가정 | `assume_bottles` (소액면세 미적용, 배송비는 병당으로 나눔) |
| 샵 추가 | `shops` 에 항목 추가. Shopify 샵은 `type: shopify` + `products.json` URL 이 가장 안정적 |
| 봇 차단되는 샵 | 해당 샵에 `fetch: playwright` + 워크플로의 playwright 설치 줄 주석 해제 |

## 알림 예시

```
Springbank 10 Year Old Local Barley 2026 Release 70cl [Local Barley]
The Whisky Exchange · 재고 있음 · 700ml

도착가(추정) ₩402,335
기준(이력 중앙값, 표본 4건) ₩507,500 대비 -20.7%

상품 GBP 99.96 -> ₩179,925 (VAT 20% 제외 가정)
배송(추정) GBP 30.00 -> ₩54,000
세금 ₩168,410 = 주세 ₩129,546 + 교육세 ₩38,864 (소액면세: 관세·부가세 면제)

https://...
```

## 알아 둘 한계

- **이 코드는 실제 샵 사이트를 대상으로 검증되지 않았습니다.** 파서, 세금 계산, 알림, 저장 로직은 로컬 가짜 서버와 픽스처로 59개 테스트를 통과했지만,
  `config.yaml` 의 4개 샵 `search_url`/`preset` 은 초기값입니다. 5단계의 `setup-check` 로 반드시 확인하세요.
  파서는 JSON-LD → 셀렉터 프리셋 → 범용 휴리스틱 순으로 시도하고, 한 샵이 실패해도 나머지는 계속 돌며, 연속 실패하면 텔레그램으로 알립니다.
- **실시간이 아니라 준실시간**입니다. GitHub 예약 실행은 지연될 수 있고 기본 간격은 30분입니다. 한정 물량이 몇 분 만에 소진되는 상품에는 부족할 수 있습니다.
- **배송비는 추정치**입니다. 실제 배송비는 장바구니에서 한국 주소로 확인해 `ship_base`/`ship_extra` 를 고쳐야 정확합니다. 샵 간 비교와 소액면세 판정에 영향이 있습니다.
- **VAT 제외·관세 20% 는 가정**입니다. 샵이 수출 시 실제로 VAT/영국 주세를 어떻게 처리하는지는 첫 실제 구매 때 체크아웃 금액과 비교해 보정하세요.
- 봇 차단(Cloudflare 등)이 있으면 requests 로는 수집되지 않을 수 있습니다. `robots.txt` 가 허용하지 않는 경로는 기본적으로 요청하지 않으며(`respect_robots`), 요청 사이에 무작위 대기를 둡니다. 각 샵의 이용약관도 확인하세요.
- 통관 규정: 자가사용 인정은 1L 이하 1병 기준이며 재판매가 의심되면 통관이 보류될 수 있습니다. 세금 적용 시점은 주문일이 아니라 입항일(환율 포함)입니다.
- 경매 사이트(Whisky Auctioneer 등)는 낙찰가·마감시간 구조가 달라 이 프로그램의 범위에 포함하지 않았습니다.
- 60일 넘게 저장소 활동이 없으면 GitHub 이 (공개 저장소의) 예약 실행을 자동 중지합니다. 이력 커밋이 정기적으로 생기면 보통 문제없지만, 중지되면 Actions 탭에서 다시 켜면 됩니다.

## 구성

```
whiskywatch/
  monitor.py     수집 -> 도착가 -> 기준가 비교 -> 알림 -> 이력 저장 (한 사이클)
  parsers.py     JSON-LD / 셀렉터 프리셋 / 휴리스틱 / Shopify JSON 파서
  normalize.py   상품명 분석(증류소·숙성·에디션·용량), 비교 키 생성, 관심 제품 판별
  tax.py         한국 수입 세금 계산
  fx.py          환율 (캐시·폴백)
  store.py       이력(JSONL)·상태(JSON)·중앙값 기준가
  fetchers.py    requests / Playwright, robots.txt, 봇 차단 감지
  notify.py      텔레그램 / 콘솔
config.yaml      키워드, 샵, 임계값, 세율
data/            history.jsonl, state.json (Actions 가 자동 커밋)
tests/           pytest (59개)
```
