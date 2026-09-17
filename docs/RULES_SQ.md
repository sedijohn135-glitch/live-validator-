# Kur sistemi thotë HYR dhe kur thotë MOS HYR

Ky është shpjegimi me fjalë të thjeshta. Nuk ka kod këtu.

## Çfarë bën sistemi
Gemini bën analizën dhe dërgon një setup. Sistemi **nuk hyn kurrë vetë në treg** dhe **nuk dërgon kurrë urdhra**.
Ai vetëm shikon çmimin live 24/7 dhe të thotë njërën nga dy gjërat:

- 🟢 **HYR TANI** — kushtet u konfirmuan. Hape tregtinë vetë në telefon, me lotin që zgjedh ti.
- ⛔ / ⌛ / ⚠️ / ❌ — **MOS HYR**. Setup-i u prish, skadoi, e humbi konfirmimin ose u refuzua që në fillim.

## Kur një setup refuzohet menjëherë (❌)
- Drejtimi nuk përputhet me bias-in e kohës së madhe.
- Nivelet janë në rend të gabuar, ose invalidimi është vendosur jashtë vendit.
- Çmimi i analizës nuk përputhet me çmimin live (Gemini ka gabuar ose ka pritur shumë).
- Nivelet janë shumë larg çmimit aktual.
- Lëvizja tashmë ka ndodhur: çmimi ka kaluar TP1.
- Raporti fitim/humbje është nën 1:2.
- SL-ja është shumë e ngushtë ose shumë e gjerë për volatilitetin e momentit.
- Checklist-i i v11 nuk mbush minimumin (p.sh. nën 7 pozitive, ose 3+ negative).
- **PDA-ja e deklaruar nuk ekziston në të dhënat reale** — ose ka dështuar tashmë.
- Likuiditeti i kundërt nuk është real, ose thuhet se u mor kur nuk u mor.
- SHORT shumë afër maksimumit historik pa konfirmim institucional.
- Nuk ka asnjë Kill Zone të vlefshme para se setup-i të skadojë.

## Kur një setup i pranuar prishet (⛔)
- Çmimi preku SL-në **para** se të hyje.
- Një qiri **mbylli trupin** përtej nivelit të invalidimit.
- Një qiri **mbylli trupin** përtej zonës (CE ose skaji i PDA-së). Fitili lejohet; trupi jo.
- Koha mbaroi (⌛ SKADOI).

## Kur sistemi thotë HYR (🟢)
Të gjitha këto duhet të jenë të vërteta në të njëjtën kohë:

1. Çmimi preku zonën e hyrjes.
2. Likuiditeti i kundërt u mor (fundi/maja u fshi).
3. Erdhi konfirmimi në kohën e vogël: CISD + një qiri displacement (ose konfirmimi specifik i modelit).
4. Konfirmimi erdhi **shpejt** pas prekjes, jo shumë qirinj më vonë.
5. Jemi brenda Kill Zone-s së modelit, jo në drekë, jo me treg të mbyllur, jo në lajme.
6. Spread-i është normal.
7. Çmimi live nuk ka ikur: hyrja është ende brenda kufirit dhe RR-ja mbetet e mirë.
8. Të dhënat janë të freskëta dhe pa ndërprerje.
9. Nuk ka një tregti të hapur në drejtim të kundërt.
10. Pikët (score) e cilësisë kalojnë minimumin.

## Kur sistemi thotë "e humbëm" (⚠️)
- **Spread i lartë** dy qirinj radhazi.
- **Çmimi iku** përtej kufirit — v11: mos e ndiq çmimin.
- Konfirmimi ndodhi gjatë një ndërprerjeje të të dhënave.
- Ishe në pauzë (`/pause`).

Në çdo dyshim përgjigja është MOS HYR. Një tregti e humbur nuk kushton; një HYR e gabuar kushton.

## Pas hyrjes
Sistemi vazhdon të shikojë dhe të njofton për TP1/TP2/TP3, SL, ose 🚪 **DIL NGA TREGU** nëse struktura prishet.
Lot, rrezik dhe vendimi përfundimtar janë gjithmonë të tutë.
