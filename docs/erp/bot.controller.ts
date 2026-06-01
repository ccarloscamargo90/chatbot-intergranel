import {
  Controller,
  Get,
  NotFoundException,
  Param,
  Query,
  UseGuards,
} from '@nestjs/common';
import { ApiOperation, ApiSecurity, ApiTags } from '@nestjs/swagger';
import { BotApiKeyGuard } from './bot-api-key.guard';
import { BotOrderDto, BotService } from './bot.service';
// Ajusta el import al decorador real de bypass del guard JWT global de tu repo
// (p. ej. @Public() en common/decorators). Si tu guard JWT no es global, puedes
// omitir este decorador.
import { Public } from '../../common/decorators/public.decorator';

@ApiTags('bot')
@ApiSecurity('bot-api-key')
@Public()
@UseGuards(BotApiKeyGuard)
@Controller({ path: 'bot/ordenes', version: '1' })
export class BotController {
  constructor(private readonly botService: BotService) {}

  @Get(':folio')
  @ApiOperation({ summary: 'Obtiene una orden (Contrato) del cliente por folio' })
  async getByFolio(@Param('folio') folio: string): Promise<BotOrderDto> {
    const order = await this.botService.getOrderByFolio(folio);
    if (!order) {
      throw new NotFoundException(`Orden ${folio} no encontrada`);
    }
    return order;
  }

  @Get()
  @ApiOperation({ summary: 'Lista las órdenes del cliente por teléfono' })
  async listByPhone(
    @Query('telefono') telefono: string,
  ): Promise<BotOrderDto[]> {
    if (!telefono) {
      return [];
    }
    return this.botService.listOrdersByPhone(telefono);
  }
}
