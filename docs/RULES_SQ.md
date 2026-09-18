# Si vendos validatori (pa kod)

Ky është shpjegimi me fjalë të thjeshta. Analiza është e jotja — validatori merret vetëm me
**momentin e hyrjes** dhe me **mbrojtjen e fitimit**.

## Çfarë bën dhe çfarë nuk bën

**Nuk bën:**
- Nuk refuzon asnjë setup. Asnjëherë. Pa marrë parasysh orën, sesionin, kill zone-n, bias-in,
  premium/discount, modelin apo çdo rregull strategjie.
- Nuk hyn në treg dhe nuk dërgon urdhra. Kurrë.
- Nuk të thotë sa lot të hedhësh.

**Bën:**
- Pret që çmimi të prekë zonën tënde.
- Aty mbledh **evidencë live** dhe të thotë **HYR TANI** ose **LIMIT** me çmim të rillogaritur.
- Rillogarit SL-në nga struktura live, mban objektivat e tua dhe të thotë ku ta **sigurosh fitimin**.

## Çfarë i duhet

Vetëm: simboli, **hyrja** (ose zona) dhe **SL**. Objektivat janë opsionale — pa to i llogarit vetë
te 1R, 2R, 3R. Drejtimin e nxjerr nga vendi i SL-së. Çdo gabim i vogël (zonë e përmbysur, objektiv
në anën e gabuar, drejtim i shkruar gabim) rregullohet dhe të raportohet, nuk refuzohet.

## Evidenca — kur thotë HYR TANI

Pas prekjes së zonës numëron pikë nga qirinjtë M1 live. **Duhen 3 pikë dhe së paku një sinjal
kryesor:**

| Sinjali | Pikë | Çfarë do të thotë |
|---|---|---|
| RECLAIM | 2 (kryesor) | Çmimi shkoi përtej zonës, mori likuiditetin, dhe u kthye brenda |
| REJECTION | 2 (kryesor) | Bisht refuzimi ose qiri gëlltitës te zona |
| SHIFT | 2 (kryesor) | Struktura mikro u thye në drejtimin tënd |
| MOMENTUM | 1 | Qiri me trup të fortë (≥ 0.9 ATR) në drejtimin tënd |
| ABSORPTION | 1 | 3 qirinj radhazi pa e humbur zonën |

Shembuj: RECLAIM + MOMENTUM = 3 ✅ · REJECTION + ABSORPTION = 3 ✅ · MOMENTUM + ABSORPTION = 2 ❌
(zona reagoi, por asgjë nuk e konfirmoi).

Kjo është pika e balancës: kurrë një sinjal i vetëm (shumë herët), kurrë gjashtë kushte (shumë vonë).

## Pritje — nuk është refuzim

Këto **e vonojnë** mesazhin HYR, nuk e vrasin setupin:
- spread i lartë (do ta paguaje ti spike-un),
- çmimi po bie/ngjitet si thikë përmes zonës,
- të dhënat live mungojnë ose janë të vjetra,
- ende s'ka qiri M1 të mbyllur pas prekjes.

## HYR TANI apo LIMIT

Nëse çmimi ka ikur më shumë se **0.35R** nga zona para se evidenca të mbushej, nuk të thotë ta ndjekësh.
Të jep një **LIMIT** me çmim të rillogaritur: 50% i qiriut të konfirmimit, ose FVG-ja M1 që u krijua,
ose buza e zonës.

## SL-ja e rillogaritur

SL = ekstremi i konfirmimit ± `max(1.5 × ATR(M1), 2 × spread, 2 tick)`.
Struktura jep nivelin, ATR jep hapësirën — që një bisht normal të mos e marrë stopin.

Kufijtë: kurrë më i gjerë se SL-ja jote, kurrë më i ngushtë se 0.35R e saj.

## Siguro fitimet

Pika ku çmimi kthehet më shpesh para TP1. Llogaritet te hyrja: swing-u më i afërt M5/M15, ose niveli
i sesionit (PDH/PDL, Azia, Londra, mesnata NY), ose numri i rrumbullakët — cilido është më afër, por
jo më larg se 1R.

Kur çmimi e prek: **🛡️ SIGURO FITIMET** — mbyll një pjesë dhe vendos SL-në te hyrja.
Pastaj: 1R → SL në BE, TP1 → SL te niveli i sigurimit, dhe nëse struktura thyhet kundër teje para
TP1 → ⚠️ paralajmërim kthimi.

## Anulimi — vetëm dy raste

1. **SL u prek para hyrjes** → setupi u anulua.
2. **TP1 u prek para hyrjes** → lëvizja iku pa ty.

Asgjë tjetër nuk e anulon. S'ka skadim, s'ka fund sesioni, s'ka lajme.

## Mesazhet që merr

📝 regjistrim · 👀 çmimi po afrohet · 🎯 zona u prek · 🔎 evidenca po ndërtohet · ✅ HYR TANI ·
⏳ LIMIT · 📥 limiti u mbush · 🛡️ siguro fitimet · 🔁 SL në BE · ⚠️ shenja kthimi · 🎯 TP · 🛑 SL ·
❌ anulim.
