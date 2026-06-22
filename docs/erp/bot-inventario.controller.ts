import {
  Controller,
  Get,
  NotFoundException,
  Param,
  UseGuards,
} from '@nestjs/common';
import { ApiOperation, ApiSecurity, ApiTags } from '@nestjs/swagger';
import { BotApiKeyGuard } from './bot-api-key.guard';
import {
  BotInventarioService,
  BotInventoryItemDto,
} from './bot-inventario.service';
// Ajusta el import al decorador real de bypass del guard JWT global de tu repo.
import { Public } from '../../common/decorators/public.decorator';

@ApiTags('bot')
@ApiSecurity('bot-api-key')
@Public()
@UseGuards(BotApiKeyGuard)
@Controller({ path: 'bot', version: '1' })
export class BotInventarioController {
  constructor(private readonly inventario: BotInventarioService) {}

  @Get('inventario/:producto')
  @ApiOperation({ summary: 'Existencia, umbral y estado de un producto' })
  async getItem(
    @Param('producto') producto: string,
  ): Promise<BotInventoryItemDto> {
    const item = await this.inventario.getInventoryItem(producto);
    if (!item) {
      throw new NotFoundException(`Producto ${producto} no está en inventario`);
    }
    return item;
  }

  @Get('inventario')
  @ApiOperation({ summary: 'Inventario completo' })
  async list(): Promise<BotInventoryItemDto[]> {
    return this.inventario.listInventory();
  }
}
