# Spotify Seed Genres

Spotify APIで利用可能なseed_genres一覧（全126種類）

最終更新: 2025-06-19 01:45:06

## ジャンル一覧


### A

- `acoustic`
- `afrobeat`
- `alt-rock`
- `alternative`
- `ambient`
- `anime`

### B

- `black-metal`
- `bluegrass`
- `blues`
- `bossanova`
- `brazil`
- `breakbeat`
- `british`

### C

- `cantopop`
- `chicago-house`
- `children`
- `chill`
- `classical`
- `club`
- `comedy`
- `country`

### D

- `dance`
- `dancehall`
- `death-metal`
- `deep-house`
- `detroit-techno`
- `disco`
- `disney`
- `drum-and-bass`
- `dub`
- `dubstep`

### E

- `edm`
- `electro`
- `electronic`
- `emo`

### F

- `folk`
- `forro`
- `french`
- `funk`

### G

- `garage`
- `german`
- `gospel`
- `goth`
- `grindcore`
- `groove`
- `grunge`
- `guitar`

### H

- `happy`
- `hard-rock`
- `hardcore`
- `hardstyle`
- `heavy-metal`
- `hip-hop`
- `holidays`
- `honky-tonk`
- `house`

### I

- `idm`
- `indian`
- `indie`
- `indie-pop`
- `industrial`
- `iranian`

### J

- `j-dance`
- `j-idol`
- `j-pop`
- `j-rock`
- `jazz`

### K

- `k-pop`
- `kids`

### L

- `latin`
- `latino`

### M

- `malay`
- `mandopop`
- `metal`
- `metal-misc`
- `metalcore`
- `minimal-techno`
- `movies`
- `mpb`

### N

- `new-age`
- `new-release`

### O

- `opera`

### P

- `pagode`
- `party`
- `philippines-opm`
- `piano`
- `pop`
- `pop-film`
- `post-dubstep`
- `power-pop`
- `progressive-house`
- `psych-rock`
- `punk`
- `punk-rock`

### R

- `r-n-b`
- `rainy-day`
- `reggae`
- `reggaeton`
- `road-trip`
- `rock`
- `rock-n-roll`
- `rockabilly`
- `romance`

### S

- `sad`
- `salsa`
- `samba`
- `sertanejo`
- `show-tunes`
- `singer-songwriter`
- `ska`
- `sleep`
- `songwriter`
- `soul`
- `soundtracks`
- `spanish`
- `study`
- `summer`
- `swedish`
- `synth-pop`

### T

- `tango`
- `techno`
- `trance`
- `trip-hop`
- `turkish`

### W

- `work-out`
- `world-music`

## 使用例

```python
# Spotifyのrecommendations APIで使用
recommendations = sp.recommendations(
    seed_genres=['j-pop', 'j-rock'],  # ジャンルを指定
    limit=20
)
```
