# DeepFrigate — instrucciones para agentes

## Git: los commits se quedan locales

Haz commits todas las veces que haga falta: la rama local es como se guarda el
avance aquí. Publicar es una decisión aparte, de una persona, fuera de la sesión
del agente.

No ejecutes `git push`. No abras pull requests ni merge requests. No crees,
muevas ni borres ramas o etiquetas remotas. Esto también descarta los comandos
que publican de rebote: `git push --tags`, un `pull --rebase` seguido de push,
`gh`/`glab` abriendo un PR, y la configuración estilo `push.autoSetupRemote` que
convierte un `git push` simple en la creación de una rama en el remoto.

Aplica a los dos repos del proyecto: este y `frigate-pg/` (fork de Frigate, rama
`deepfrigate/pgsql`).

Cuando una rama esté lista para compartir, dilo, nombra la rama y ahí te paras.

## El resto de las reglas de la casa

Están en `docs/HANDOFF-FRONT.md` §9 y en `HANDOFF.md`: no tocar el checkout
upstream `frigate/`, nada de secretos en git, commits en español con los
trailers `Co-Authored-By` y `Claude-Session`, y pruebas por servicio en su
imagen `-test`.
