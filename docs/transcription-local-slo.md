# SLO de Latência — Engine Local de Transcrição (faster-whisper)

**Issue:** #35 | **Deps:** #29 (engine local), #23 (fixtures)

---

## Métrica

**RTF (Real-Time Factor)** = `tempo_de_processamento / duração_do_áudio`

- RTF = 0.5 significa que 1 segundo de áudio demora 0,5 s para transcrever.
- Reportado como **p50/p95** por device, medido por `scripts/bench_transcription_local.py`.

---

## Benchmark (AC-S1/AC-S4)

O script `scripts/bench_transcription_local.py` usa os fixtures versionados de
`tests/fixtures/transcription/clips/` (≥5 clipes criados por #23 AC-2).

**Clipe de referência do SLO:** qualquer fixture nomeado em `clips/`, com duração,
formato e sample-rate documentados no `tests/fixtures/transcription/README.md`.

**Rodar (local/manual — fora do CI):**
```bash
LOCAL_MODEL_PATH=/path/to/ctranslate2-model \
python3 scripts/bench_transcription_local.py \
  --device cpu \
  --local_timeout_seconds 60 \
  --max_duration_seconds 120
```

O script **não entra no CI** — resultado é dependente de hardware e não-determinístico
entre execuções (declarado no cabeçalho do script).

---

## Target SLO — desigualdade derivada (AC-S2)

O target **não** é um número arbitrário: é uma desigualdade derivada da config existente:

```
p95_RTF × max_duration_seconds ≤ local_timeout_seconds
```

Onde:
- `max_duration_seconds` (default 120s) — cap de duração V1 (#19 AC-4)
- `local_timeout_seconds` (default 60s) — timeout duro de #29 AC-6

**O SLO falha** se a desigualdade não se sustentar com os defaults. Nesse caso,
a recomendação abaixo deve ser aplicada antes de habilitar GPU em produção.

### Valores de referência por device (a serem preenchidos após benchmark real)

| device | p50 RTF | p95 RTF | p95 × 120s | Cabe em 60s? |
|--------|---------|---------|------------|--------------|
| cpu    | TBD     | TBD     | TBD        | TBD          |
| cuda   | TBD     | TBD     | TBD        | TBD          |

> Execute `scripts/bench_transcription_local.py` em hardware representativo e
> preencha esta tabela antes do deploy em produção.

---

## Enforcement em runtime (AC-S3)

O enforcement do SLO **é o timeout duro `local_timeout_seconds`** já entregue
por #29 AC-6 — não há subsistema novo.

**Mapeamento SLO → config:**

```
duração_máxima_recomendada_por_device = floor(local_timeout_seconds / p95_RTF)
```

Exemplos (com `local_timeout_seconds=60`):

| device | p95 RTF (exemplo) | max_duration_recomendado |
|--------|-------------------|--------------------------|
| cpu    | 0.80              | floor(60 / 0.80) = 75s   |
| cuda   | 0.15              | floor(60 / 0.15) = 400s  |

> Estes são valores **ilustrativos**. Substitua por valores medidos do benchmark.

**Se `p95_RTF × max_duration_seconds > local_timeout_seconds`**, ajuste um dos dois:
- Aumente `local_timeout_seconds` para ≥ `ceil(p95_RTF × max_duration_seconds)`, ou
- Reduza `max_duration_seconds` para ≤ `floor(local_timeout_seconds / p95_RTF)`.

---

## GPU toggle (AC-G1/AC-G2/AC-G4)

### Config

```yaml
# config/deilebot.yaml
transcription:
  engine: local
  local_device: cpu        # ou cuda para GPU
  local_compute_type: int8 # float16 recomendado para GPU
  local_cuda_fallback: fail # fail (default) | cpu
  local_timeout_seconds: 60
  local_model_path: /path/to/ctranslate2-model
```

### Fallback de CUDA (AC-G2)

| `local_device` | `local_cuda_fallback` | CUDA disponível? | Comportamento |
|----------------|-----------------------|-----------------|---------------|
| `cpu`          | qualquer              | N/A             | CPU (normal)  |
| `cuda`         | `fail` (default)      | sim             | CUDA          |
| `cuda`         | `fail` (default)      | não             | Erro claro: "CUDA indisponível" |
| `cuda`         | `cpu`                 | não             | WARN `device_fallback=cuda->cpu`, segue em CPU |

### Log de device (AC-G1)

Ao carregar o modelo, o backend emite log INFO com `device=<cpu|cuda>`:

```
{"level": "info", "msg": "local model loaded", "device": "cpu", ...}
```

Com `local_cuda_fallback=cpu` e CUDA indisponível, emite WARN antes:

```
{"level": "warning", "msg": "CUDA indisponível — usando CPU", "device_fallback": "cuda->cpu", ...}
```

### Smoke test de GPU (AC-G4 — MANUAL, sem GPU runner em CI)

Checklist a executar em nó com GPU antes de deploy em produção:

- [ ] Configura `local_device: cuda`, `local_cuda_fallback: fail`
- [ ] Inicia o bot; verifica log: `{"device": "cuda"}` presente em `"local model loaded"`
- [ ] Transcreve um clipe de referência; verifica resposta correta
- [ ] Altera para `local_device: cpu`; reinicia; verifica log: `{"device": "cpu"}`
- [ ] Com `local_device: cuda` e CUDA desabilitada (ex: sem driver): verifica erro explícito "CUDA indisponível"
- [ ] Com `local_cuda_fallback: cpu` e CUDA desabilitada: verifica WARN `device_fallback=cuda->cpu` e transcrição continua

**Não há GPU runner em CI — este smoke test não é automatizado por design.**

---

## Entrega cross-repo (AC-G3)

O scheduling de GPU no Kubernetes (resource limits `nvidia.com/gpu`, nodeSelector,
tolerations, tag de imagem CUDA) **vive no repo `deile`, em `infra/k8s/`**
(ver `README.md:165`). Não há manifesto K8s neste repo.

AC-G3 é rastreado como checklist line na issue #35 e deve ser entregue no repo `deile`
como perfil GPU gated por toggle (default OFF), verificável por diff de render.

---

## AC-8 — Benchmark de WER (manual, fora do CI)

**Issue:** #49 | **Dependência:** áudio e credencial reais (#38, human-gated)

### O que é WER

**WER (Word Error Rate)** = `(S + D + I) / N` onde S=substituições, D=deleções,
I=inserções e N=número de palavras na referência.

- WER = 0,00 → transcrição perfeita
- WER = 0,15 → 15% das palavras com erro (threshold de aceite)

### Threshold de aceite

```
WER ≤ 0,15  (acurácia ≥ 85%)
```

### Comando exato

```bash
LOCAL_MODEL_PATH=/path/to/ctranslate2-model \
python3 tests/fixtures/transcription/benchmark_local.py \
  --device cpu \
  --compute_type int8 \
  --language pt
```

Onde `LOCAL_MODEL_PATH` aponta para um diretório CTranslate2 válido
(contendo `model.bin`, `config.json`, `tokenizer.json`).

### Fixtures de referência

Os clipes e transcrições de referência ficam em
`tests/fixtures/transcription/clips/` (criados via issue #23):

```
tests/fixtures/transcription/clips/
├── pt_01.ogg        ← clipe PT-BR
├── pt_01.ref.txt    ← transcrição de referência (sem pontuação, minúsculas)
├── pt_02.ogg
├── pt_02.ref.txt
├── en_01.ogg        ← clipe EN
├── en_01.ref.txt
└── ...
```

Cada `.ref.txt` contém a transcrição esperada normalizada:
sem pontuação opcional, tudo em minúsculas, sem acentos opcionais.

### Ferramenta de WER

Use `jiwer` (pip install jiwer) ou o script em `tests/fixtures/transcription/benchmark_local.py`:

```python
from jiwer import wer
score = wer(reference, hypothesis)
assert score <= 0.15, f"WER {score:.2%} > threshold 15%"
```

### Nota: por que não roda no CI

- Requer modelo local (`LOCAL_MODEL_PATH`) — não versionado no repo.
- Requer áudio com voz real — os arquivos `.ogg` em `clips/` devem ser
  adicionados manualmente (issue #38 é human-gated por esta razão).
- Resultado não-determinístico entre execuções (temperatura > 0).

Linkar a partir de `README.md` (âncora `#transcrição-local`) — ver #49.
